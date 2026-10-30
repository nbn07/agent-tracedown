"""OpenAI Agents SDK.

    from agent_tracedown.integrations import openai_agents as tdn_oa
    tdn_oa.instrument()                 # add alongside OpenAI's own tracing
    tdn_oa.instrument(replace=True)     # or make Tracedown the only processor

Each SDK trace becomes one TDN run. Agent spans become agents (their tools and
handoffs declared), generation/response spans become LLM calls, function spans become
tool calls, handoff spans become handoffs and guardrails become decisions.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from ..tracer import ManualTrace
from . import maybe_json, to_jsonable

try:
    from agents.tracing import TracingProcessor as _Base
except ImportError:  # pragma: no cover
    _Base = object  # type: ignore[assignment,misc]


def _response_text(resp: Any) -> Optional[str]:
    text = getattr(resp, "output_text", None)
    if text:
        return text
    parts = []
    for item in getattr(resp, "output", None) or []:
        for c in getattr(item, "content", None) or []:
            if getattr(c, "text", None):
                parts.append(c.text)
    return "".join(parts) or None


class TracedownProcessor(_Base):  # type: ignore[misc,valid-type]
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.traces: Dict[str, ManualTrace] = {}
        self.last_output: Dict[str, Any] = {}
        self.trace_ids = []

    # ------------------------------------------------------------------ traces
    def on_trace_start(self, trace) -> None:
        with self._lock:
            t = ManualTrace(getattr(trace, "name", None) or "Agent workflow",
                            **{"openai.trace_id": getattr(trace, "trace_id", None)})
            self.traces[trace.trace_id] = t
            self.trace_ids.append(t.trace_id)

    def on_trace_end(self, trace) -> None:
        with self._lock:
            t = self.traces.pop(trace.trace_id, None)
            if t is not None:
                t.finish(output=self.last_output.pop(trace.trace_id, None))

    # ------------------------------------------------------------------ spans
    def on_span_start(self, span) -> None:
        with self._lock:
            t = self.traces.get(span.trace_id)
            if t is None:
                return
            d, kind_name = span.span_data, type(span.span_data).__name__
            parent = span.parent_id
            if kind_name == "AgentSpanData":
                t.start(span.span_id, f"invoke_agent {d.name}", "agent", parent=parent,
                        attrs={"gen_ai.agent.name": d.name, "tdn.agent.tools": list(d.tools or []),
                               "tdn.agent.handoffs": list(d.handoffs or [])})
                if d.tools and not t.root.attributes.get("tdn.tools.available"):
                    t.declare_tools([{"name": n} for n in d.tools])
            elif kind_name in ("GenerationSpanData", "ResponseSpanData"):
                t.start(span.span_id, "chat", "llm", parent=parent)
            elif kind_name == "FunctionSpanData":
                t.start(span.span_id, f"execute_tool {d.name}", "tool", parent=parent,
                        input=maybe_json(d.input), attrs={"gen_ai.tool.name": d.name})
            elif kind_name == "HandoffSpanData":
                t.start(span.span_id, f"handoff {d.from_agent} -> {d.to_agent}", "handoff", parent=parent,
                        attrs={"tdn.handoff.from": d.from_agent, "tdn.handoff.to": d.to_agent})
            elif kind_name == "GuardrailSpanData":
                t.start(span.span_id, f"decision guardrail {d.name}", "decision", parent=parent,
                        attrs={"tdn.decision.options": ["pass", "tripwire"]})

    def on_span_end(self, span) -> None:
        with self._lock:
            t = self.traces.get(span.trace_id)
            if t is None or not t.has(span.span_id):
                return
            d, kind_name = span.span_data, type(span.span_data).__name__
            err = span.error
            error = f"{err.get('message')}: {err.get('data')}" if isinstance(err, dict) else (str(err) if err else None)
            if kind_name == "GenerationSpanData":
                usage = d.usage or {}
                text = to_jsonable(d.output)
                t.spans[span.span_id].name = f"chat {d.model or 'model'}"
                t.set(span.span_id, **{"gen_ai.request.model": d.model, "gen_ai.provider.name": "openai"})
                self._input(t, span.span_id, d.input)
                t.end(span.span_id, output={"text": text}, error=error,
                      **{"gen_ai.usage.input_tokens": usage.get("input_tokens"),
                         "gen_ai.usage.output_tokens": usage.get("output_tokens")})
                self.last_output[span.trace_id] = text
            elif kind_name == "ResponseSpanData":
                resp = d.response
                model = getattr(resp, "model", None)
                usage = getattr(resp, "usage", None)
                text = _response_text(resp)
                if model:
                    t.spans[span.span_id].name = f"chat {model}"
                self._input(t, span.span_id, d.input)
                t.end(span.span_id, output={"text": text}, error=error,
                      **{"gen_ai.request.model": model, "gen_ai.provider.name": "openai",
                         "gen_ai.usage.input_tokens": getattr(usage, "input_tokens", None),
                         "gen_ai.usage.output_tokens": getattr(usage, "output_tokens", None)})
                if text:
                    self.last_output[span.trace_id] = text
            elif kind_name == "FunctionSpanData":
                # the SDK fills in the arguments after the span starts
                if d.input is not None and t.spans[span.span_id].input_digest is None:
                    self._input(t, span.span_id, maybe_json(d.input))
                t.end(span.span_id, output=maybe_json(to_jsonable(d.output)), error=error)
            elif kind_name == "HandoffSpanData":
                # the target agent is only known once the handoff resolves
                t.spans[span.span_id].name = f"handoff {d.from_agent} -> {d.to_agent}"
                t.end(span.span_id, error=error, **{"tdn.handoff.from": d.from_agent, "tdn.handoff.to": d.to_agent})
            elif kind_name == "GuardrailSpanData":
                chosen = "tripwire" if d.triggered else "pass"
                t.end(span.span_id, output=chosen, error=error,
                      **{"tdn.decision.chosen": chosen, "tdn.guardrail.triggered": bool(d.triggered)})
            else:
                t.end(span.span_id, error=error)

    @staticmethod
    def _input(t: ManualTrace, key: str, value: Any) -> None:
        if value is not None:
            from ..tracer import _put_blob
            t.spans[key].input_digest = _put_blob(t.state, to_jsonable(value))

    def shutdown(self) -> None:
        with self._lock:
            for tid in list(self.traces):
                self.traces.pop(tid).finish()

    def force_flush(self) -> None:
        pass


def instrument(replace: bool = False) -> TracedownProcessor:
    from agents.tracing import add_trace_processor, set_trace_processors
    proc = TracedownProcessor()
    if replace:
        set_trace_processors([proc])
    else:
        add_trace_processor(proc)
    return proc
