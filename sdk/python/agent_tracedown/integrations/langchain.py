"""LangChain and LangGraph.

    from agent_tracedown.integrations import langchain as tdn_lc
    graph.invoke(state, config={"callbacks": [tdn_lc.callback("SupportGraph")]})

Each LangGraph node becomes an agent span (``langgraph_node`` metadata), with the
LLM calls, tool calls and retrievals inside it; moving from one node to the next
records a handoff. Internal runnables (sequences, channel writes) are skipped so the
trace shows the agent structure, not plumbing. One handler can serve many runs.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional
from uuid import UUID

from ..tracer import ManualTrace
from . import maybe_json, to_jsonable

try:  # LangChain checks isinstance(handler, BaseCallbackHandler) on some paths
    from langchain_core.callbacks import BaseCallbackHandler as _Base
except ImportError:  # pragma: no cover - framework not installed
    _Base = object  # type: ignore[assignment,misc]


def _final_text(outputs: Any) -> Any:
    if isinstance(outputs, dict):
        msgs = outputs.get("messages")
        if isinstance(msgs, list) and msgs:
            return to_jsonable(getattr(msgs[-1], "content", msgs[-1]))
        for k in ("output", "answer", "result", "response"):
            if k in outputs:
                return to_jsonable(outputs[k])
    return to_jsonable(getattr(outputs, "content", outputs))


def _model_name(serialized: Optional[Dict[str, Any]], kwargs: Dict[str, Any]) -> str:
    inv = kwargs.get("invocation_params") or {}
    meta = kwargs.get("metadata") or {}
    return str(inv.get("model") or inv.get("model_name") or meta.get("ls_model_name") or
               ((serialized or {}).get("kwargs") or {}).get("model_name") or
               ((serialized or {}).get("id") or ["llm"])[-1])


class TracedownCallbackHandler(_Base):  # type: ignore[misc,valid-type]
    raise_error = False
    run_inline = True

    def __init__(self, name: Optional[str] = None, **run_attrs: Any) -> None:
        if _Base is not object:
            super().__init__()
        self.name, self.run_attrs = name, run_attrs
        self._lock = threading.RLock()
        self._trace_of: Dict[str, ManualTrace] = {}     # any run_id -> its trace
        self._key_of: Dict[str, Optional[str]] = {}      # run_id -> nearest TDN span key (None = root)
        self._root_run: Dict[str, str] = {}              # trace_id -> top-level run_id
        self._last_node: Dict[str, str] = {}             # trace_id -> last finished node name
        self.trace_ids: List[str] = []

    # ------------------------------------------------------------------ helpers
    def _resolve(self, run_id: UUID, parent_run_id: Optional[UUID]):
        rid, pid = str(run_id), str(parent_run_id) if parent_run_id else None
        trace = self._trace_of.get(pid) if pid else None
        parent_key = self._key_of.get(pid) if pid else None
        return rid, trace, parent_key

    def _start(self, run_id, parent_run_id, name, kind, input=None, attrs=None):
        with self._lock:
            rid, trace, parent_key = self._resolve(run_id, parent_run_id)
            if trace is None:  # an LLM or tool called outside any chain: its own run
                trace = ManualTrace(self.name or name, input=input, **self.run_attrs)
                self._root_run[trace.trace_id] = rid
                self.trace_ids.append(trace.trace_id)
            trace.start(rid, name, kind, parent=parent_key, input=input, attrs=attrs)
            self._trace_of[rid], self._key_of[rid] = trace, rid
            return trace

    def _end(self, run_id, output=None, error=None, **attrs):
        with self._lock:
            rid = str(run_id)
            trace = self._trace_of.get(rid)
            if trace is None:
                return
            trace.end(rid, output=output, error=error, **attrs)
            if self._root_run.get(trace.trace_id) == rid:
                trace.finish(output=output if error is None else None, error=error)

    # ------------------------------------------------------------------ chains / graph nodes
    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, tags=None, metadata=None, **kw):
        with self._lock:
            rid, trace, parent_key = self._resolve(run_id, parent_run_id)
            node = (metadata or {}).get("langgraph_node")
            if trace is None:
                name = self.name or kw.get("name") or ((serialized or {}).get("name") if serialized else None) or "LangGraph"
                trace = ManualTrace(name, input=_final_text(to_jsonable(inputs)), **self.run_attrs)
                self.trace_ids.append(trace.trace_id)
                self._root_run[trace.trace_id] = rid
                self._trace_of[rid], self._key_of[rid] = trace, None
                return
            self._trace_of[rid] = trace
            parent_node = self._key_of.get(str(parent_run_id))
            is_new_node = node and not (parent_node and trace.spans.get(parent_node) is not None and
                                        trace.spans[parent_node].attributes.get("tdn.langgraph.node") == node)
            if is_new_node and not str(node).startswith("__"):
                prev = self._last_node.get(trace.trace_id)
                if prev and prev != node:
                    trace.start(f"{rid}:handoff", f"handoff {prev} -> {node}", "handoff", parent=parent_key,
                                attrs={"tdn.handoff.from": prev, "tdn.handoff.to": node})
                    trace.end(f"{rid}:handoff")
                trace.start(rid, f"invoke_agent {node}", "agent", parent=parent_key, input=to_jsonable(inputs),
                            attrs={"tdn.langgraph.node": node, "tdn.langgraph.step": (metadata or {}).get("langgraph_step")})
                self._key_of[rid] = rid
            else:
                self._key_of[rid] = parent_key  # plumbing: children attach to the nearest real span

    def on_chain_end(self, outputs, *, run_id, **kw):
        with self._lock:
            rid = str(run_id)
            trace = self._trace_of.get(rid)
            if trace is None:
                return
            if self._key_of.get(rid) == rid:
                span = trace.end(rid, output=to_jsonable(outputs))
                node = span.attributes.get("tdn.langgraph.node") if span else None
                if node:
                    self._last_node[trace.trace_id] = node
            if self._root_run.get(trace.trace_id) == rid:
                trace.finish(output=_final_text(outputs))

    def on_chain_error(self, error, *, run_id, **kw):
        with self._lock:
            rid = str(run_id)
            trace = self._trace_of.get(rid)
            if trace is None:
                return
            msg = f"{type(error).__name__}: {error}"
            if self._key_of.get(rid) == rid:
                trace.end(rid, error=msg)
            if self._root_run.get(trace.trace_id) == rid:
                trace.finish(error=msg)

    # ------------------------------------------------------------------ models
    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kw):
        model = _model_name(serialized, kw)
        inv = kw.get("invocation_params") or {}
        trace = self._start(run_id, parent_run_id, f"chat {model}", "llm", input=to_jsonable(messages),
                            attrs={"gen_ai.request.model": model, "gen_ai.provider.name": (kw.get("metadata") or {}).get("ls_provider"),
                                   "gen_ai.request.temperature": inv.get("temperature")})
        tools = inv.get("tools") or []
        if tools and not trace.root.attributes.get("tdn.tools.available"):
            trace.declare_tools([{"name": (t.get("function") or t).get("name"),
                                  "description": (t.get("function") or t).get("description"),
                                  "schema": (t.get("function") or t).get("parameters")} for t in tools if isinstance(t, dict)])

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, **kw):
        model = _model_name(serialized, kw)
        self._start(run_id, parent_run_id, f"chat {model}", "llm", input=prompts,
                    attrs={"gen_ai.request.model": model, "gen_ai.operation.name": "text_completion"})

    def on_llm_end(self, response, *, run_id, **kw):
        gen = response.generations[0][0] if getattr(response, "generations", None) and response.generations[0] else None
        msg = getattr(gen, "message", None)
        text = getattr(gen, "text", None) or getattr(msg, "content", None)
        tool_calls = [{"name": tc.get("name"), "arguments": tc.get("args")} for tc in (getattr(msg, "tool_calls", None) or [])]
        usage = getattr(msg, "usage_metadata", None) or ((getattr(response, "llm_output", None) or {}).get("token_usage")) or {}
        out = {"text": to_jsonable(text), "tool_calls": tool_calls} if tool_calls else {"text": to_jsonable(text)}
        self._end(run_id, output=out,
                  **{"gen_ai.usage.input_tokens": usage.get("input_tokens", usage.get("prompt_tokens")),
                     "gen_ai.usage.output_tokens": usage.get("output_tokens", usage.get("completion_tokens"))})

    def on_llm_error(self, error, *, run_id, **kw):
        self._end(run_id, error=f"{type(error).__name__}: {error}")

    # ------------------------------------------------------------------ tools
    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, inputs=None, **kw):
        name = (serialized or {}).get("name") or kw.get("name") or "tool"
        args = to_jsonable(inputs) if inputs is not None else maybe_json(input_str)
        self._start(run_id, parent_run_id, f"execute_tool {name}", "tool", input=args,
                    attrs={"gen_ai.tool.name": name})

    def on_tool_end(self, output, *, run_id, **kw):
        out = getattr(output, "content", output)
        status = getattr(output, "status", None)
        if status == "error":
            self._end(run_id, error=str(out)[:500])
        else:
            self._end(run_id, output=maybe_json(to_jsonable(out)))

    def on_tool_error(self, error, *, run_id, **kw):
        self._end(run_id, error=f"{type(error).__name__}: {error}")

    # ------------------------------------------------------------------ retrievers
    def on_retriever_start(self, serialized, query, *, run_id, parent_run_id=None, **kw):
        source = (serialized or {}).get("name") or kw.get("name") or "retriever"
        self._start(run_id, parent_run_id, "retrieval", "retrieval", input=query,
                    attrs={"tdn.retrieval.query": query, "tdn.retrieval.source": source})

    def on_retriever_end(self, documents, *, run_id, **kw):
        chunks = []
        for i, d in enumerate(documents or []):
            meta = getattr(d, "metadata", {}) or {}
            chunks.append({"id": str(getattr(d, "id", None) or meta.get("id") or f"doc{i}"),
                           "text": getattr(d, "page_content", str(d)), "source": meta.get("source"),
                           "score": meta.get("score") or meta.get("relevance_score"),
                           "version": meta.get("version"), "stale": meta.get("stale")})
        self._end(run_id, output=[{k: v for k, v in c.items() if v is not None} for c in chunks])

    def on_retriever_error(self, error, *, run_id, **kw):
        self._end(run_id, error=f"{type(error).__name__}: {error}")


def callback(name: Optional[str] = None, **run_attrs: Any) -> TracedownCallbackHandler:
    """A callback handler to pass in ``config={"callbacks": [...]}``."""
    return TracedownCallbackHandler(name, **run_attrs)
