"""LlamaIndex (``llama-index-core`` 0.10+ instrumentation).

    from agent_tracedown.integrations import llamaindex as tdn_li
    tdn_li.instrument()
    query_engine.query("...")                 # or agent.chat / workflow runs

Each top-level LlamaIndex operation (a query, a chat, an agent run) becomes one TDN
run. Retrievals keep node ids, text, scores and source file; LLM calls keep messages,
response and token usage; agent tool calls become tool spans.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from ..tracer import ManualTrace
from . import to_jsonable

_installed: Dict[str, Any] = {}


def _usage(response: Any) -> Dict[str, Any]:
    raw = getattr(response, "raw", None)
    u = (raw or {}).get("usage") if isinstance(raw, dict) else getattr(raw, "usage", None)
    if u is None:
        return {}
    get = (lambda k: u.get(k)) if isinstance(u, dict) else (lambda k: getattr(u, k, None))
    return {"gen_ai.usage.input_tokens": get("prompt_tokens") or get("input_tokens"),
            "gen_ai.usage.output_tokens": get("completion_tokens") or get("output_tokens")}


class _State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.parent: Dict[str, Optional[str]] = {}     # LlamaIndex span -> parent span
        self.trace: Dict[str, ManualTrace] = {}        # LlamaIndex span -> TDN trace
        self.roots: Dict[str, ManualTrace] = {}        # top-level LlamaIndex span -> trace
        self.trace_ids = []

    def root_of(self, span_id: Optional[str]) -> Optional[ManualTrace]:
        return self.trace.get(span_id) if span_id else None


def instrument() -> _State:
    """Register span and event handlers on the root dispatcher (idempotent)."""
    if "state" in _installed:
        return _installed["state"]
    from llama_index.core.instrumentation import get_dispatcher
    from llama_index.core.instrumentation.event_handlers import BaseEventHandler
    from llama_index.core.instrumentation.span_handlers import BaseSpanHandler

    st = _State()

    class SpanHandler(BaseSpanHandler[Any]):  # type: ignore[misc]
        @classmethod
        def class_name(cls) -> str:
            return "TracedownSpanHandler"

        def new_span(self, id_, bound_args, instance=None, parent_span_id=None, tags=None, **kw):
            with st.lock:
                st.parent[id_] = parent_span_id
                t = st.trace.get(parent_span_id) if parent_span_id else None
                if t is None:
                    op = id_.split("-")[0]  # "RetrieverQueryEngine.query"
                    first = next(iter(bound_args.arguments.values()), None) if bound_args else None
                    t = ManualTrace(op, input=to_jsonable(getattr(first, "query_str", first)))
                    st.roots[id_] = t
                    st.trace_ids.append(t.trace_id)
                st.trace[id_] = t
            return None

        def prepare_to_exit_span(self, id_, bound_args, instance=None, result=None, **kw):
            with st.lock:
                t = st.roots.pop(id_, None)
                if t is not None:
                    out = getattr(result, "response", None) or getattr(getattr(result, "message", None), "content", None)
                    t.finish(output=to_jsonable(out if out is not None else result))
            return None

        def prepare_to_drop_span(self, id_, bound_args, instance=None, err=None, **kw):
            with st.lock:
                t = st.roots.pop(id_, None)
                if t is not None:
                    t.finish(error=f"{type(err).__name__}: {err}" if err else "dropped")
            return None

    class EventHandler(BaseEventHandler):  # type: ignore[misc]
        @classmethod
        def class_name(cls) -> str:
            return "TracedownEventHandler"

        def handle(self, event, **kw):
            name = type(event).__name__
            with st.lock:
                t = st.trace.get(event.span_id)
                if t is None:
                    return
                key = f"{name.replace('Start', '').replace('End', '')}:{event.span_id}"
                if name in ("LLMChatStartEvent", "LLMCompletionStartEvent"):
                    model = (getattr(event, "model_dict", None) or {}).get("model") or \
                        (getattr(event, "model_dict", None) or {}).get("model_name") or "llm"
                    inp = to_jsonable(getattr(event, "messages", None) or getattr(event, "prompt", None))
                    t.start(key, f"chat {model}", "llm", input=inp, attrs={"gen_ai.request.model": model})
                elif name in ("LLMChatEndEvent", "LLMCompletionEndEvent"):
                    resp = getattr(event, "response", None)
                    text = getattr(getattr(resp, "message", None), "content", None) or getattr(resp, "text", None)
                    t.end(key, output={"text": text}, **_usage(resp))
                elif name == "RetrievalStartEvent":
                    q = event.str_or_query_bundle
                    q = getattr(q, "query_str", q)
                    t.start(key, "retrieval", "retrieval", input=q,
                            attrs={"tdn.retrieval.query": q, "tdn.retrieval.source": "llamaindex"})
                elif name == "RetrievalEndEvent":
                    chunks = []
                    for nws in event.nodes or []:
                        node = getattr(nws, "node", nws)
                        meta = getattr(node, "metadata", {}) or {}
                        chunks.append({k: v for k, v in {
                            "id": getattr(node, "node_id", None), "text": node.get_content() if hasattr(node, "get_content") else str(node),
                            "score": getattr(nws, "score", None), "source": meta.get("file_name") or meta.get("source"),
                            "version": meta.get("version"), "stale": meta.get("stale")}.items() if v is not None})
                    t.end(key, output=chunks)
                elif name == "AgentToolCallEvent":
                    tool = getattr(event.tool, "name", "tool")
                    tkey = f"tool:{event.id_}"
                    t.start(tkey, f"execute_tool {tool}", "tool", input=to_jsonable(event.arguments),
                            attrs={"gen_ai.tool.name": tool})
                    t.end(tkey)
                elif name == "ExceptionEvent":
                    t.root.set("tdn.llamaindex.exception", str(event.exception)[:500])

    d = get_dispatcher()
    d.add_span_handler(SpanHandler())
    d.add_event_handler(EventHandler())
    _installed["state"] = st
    return st
