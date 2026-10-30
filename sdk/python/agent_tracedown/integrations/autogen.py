"""AutoGen AgentChat (0.4+).

    from agent_tracedown.integrations import autogen as tdn_ag
    async for msg in tdn_ag.trace_stream(team.run_stream(task=task), name="SupportTeam", task=task):
        ...                                  # messages pass through unchanged

or, after the fact: ``tdn_ag.trace_result(task_result, name="SupportTeam", task=task)``.

Each change of speaker opens an agent span; model turns become LLM spans (with token
usage), ``ToolCallRequestEvent`` / ``ToolCallExecutionEvent`` pairs become tool spans
matched by call id, and ``HandoffMessage`` becomes a handoff. AutoGen Core also emits
OpenTelemetry spans; point its tracer provider at ``/v1/traces`` to keep those too.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Iterable, Optional

from ..tracer import ManualTrace
from . import maybe_json, to_jsonable


class _Builder:
    def __init__(self, name: str, task: Any = None) -> None:
        self.t = ManualTrace(name, input=to_jsonable(task) if task is not None else None)
        self.agent_key: Optional[str] = None
        self.agent: Optional[str] = None
        self.n = 0
        self.final: Any = None
        self.error: Optional[str] = None

    def _speaker(self, source: str) -> Optional[str]:
        if source == "user":
            return None
        if source != self.agent:
            if self.agent_key:
                self.t.end(self.agent_key)
            self.n += 1
            self.agent_key = f"agent:{self.n}"
            self.t.start(self.agent_key, f"invoke_agent {source}", "agent", attrs={"gen_ai.agent.name": source})
            self.agent = source
        return self.agent_key

    def _llm(self, parent: Optional[str], msg: Any, output: Any) -> None:
        usage = getattr(msg, "models_usage", None)
        if usage is None:
            return  # no model call behind this message (e.g. a tool summary)
        self.n += 1
        key = f"llm:{self.n}"
        self.t.start(key, "chat", "llm", parent=parent)
        self.t.end(key, output={"text": to_jsonable(output)},
                   **{"gen_ai.usage.input_tokens": getattr(usage, "prompt_tokens", None),
                      "gen_ai.usage.output_tokens": getattr(usage, "completion_tokens", None)})

    def feed(self, msg: Any) -> None:
        kind = type(msg).__name__
        if kind == "TaskResult":
            if getattr(msg, "stop_reason", None):
                self.t.root.set("tdn.autogen.stop_reason", msg.stop_reason)
            return
        source = getattr(msg, "source", "agent")
        parent = self._speaker(source)
        content = getattr(msg, "content", None)
        if kind == "ToolCallRequestEvent":
            self._llm(parent, msg, [{"name": c.name, "arguments": maybe_json(c.arguments)} for c in content])
            for call in content:
                self.t.start(f"tool:{call.id}", f"execute_tool {call.name}", "tool", parent=parent,
                             input=maybe_json(call.arguments), attrs={"gen_ai.tool.name": call.name})
        elif kind == "ToolCallExecutionEvent":
            for res in content:
                self.t.end(f"tool:{res.call_id}", output=None if res.is_error else maybe_json(res.content),
                           error=str(res.content)[:500] if res.is_error else None)
        elif kind == "HandoffMessage":
            self.n += 1
            key = f"handoff:{self.n}"
            self.t.start(key, f"handoff {source} -> {msg.target}", "handoff", parent=parent,
                         input=to_jsonable(content), attrs={"tdn.handoff.from": source, "tdn.handoff.to": msg.target})
            self.t.end(key)
        elif kind in ("TextMessage", "StructuredMessage", "ToolCallSummaryMessage", "ThoughtEvent"):
            if source == "user":
                if self.t.root.input_digest is None:
                    from ..tracer import _put_blob
                    self.t.root.input_digest = _put_blob(self.t.state, to_jsonable(content))
                return
            if kind != "ToolCallSummaryMessage":
                self._llm(parent, msg, content)
            if kind != "ThoughtEvent":
                self.final = to_jsonable(content)

    def finish(self) -> str:
        if self.agent_key:
            self.t.end(self.agent_key)
        return self.t.finish(output=self.final, error=self.error)


async def trace_stream(stream: AsyncIterator[Any], name: str = "AutoGenTeam", task: Any = None) -> AsyncIterator[Any]:
    b = _Builder(name, task)
    try:
        async for msg in stream:
            b.feed(msg)
            yield msg
    except Exception as exc:
        b.error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        b.finish()


def trace_result(result: Any, name: str = "AutoGenTeam", task: Any = None) -> str:
    """Record a finished ``TaskResult`` (or any iterable of messages). Returns the trace id."""
    b = _Builder(name, task)
    messages: Iterable[Any] = getattr(result, "messages", result)
    for m in messages:
        b.feed(m)
    if type(result).__name__ == "TaskResult":
        b.feed(result)
    return b.finish()
