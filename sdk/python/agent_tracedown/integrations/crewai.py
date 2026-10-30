"""CrewAI.

    from agent_tracedown.integrations import crewai as tdn_crew
    listener = tdn_crew.instrument()
    crew.kickoff(inputs={...})

A crew kickoff is one TDN run. Each agent's execution of a task is an agent span
(consecutive agents record a handoff), LLM calls and tool usages nest inside it.
CrewAI delivers events on worker threads, so spans are matched by the ids CrewAI
puts on its events (``call_id``, ``started_event_id``, ``parent_event_id``).
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from ..tracer import ManualTrace
from . import maybe_json, to_jsonable

try:
    from crewai.events import BaseEventListener as _Base
except ImportError:  # pragma: no cover
    _Base = object  # type: ignore[assignment,misc]


def _text(x: Any) -> Any:
    raw = getattr(x, "raw", None)
    return raw if isinstance(raw, str) else to_jsonable(x)


class TracedownListener(_Base):  # type: ignore[misc,valid-type]
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.active: Optional[ManualTrace] = None       # current crew run
        self.trace_ids: List[str] = []
        self._event_trace: Dict[str, ManualTrace] = {}
        self._agent_key: Dict[str, str] = {}             # agent_id|task_id -> span key
        self._tool_open: Dict[str, List[str]] = {}       # agent|tool -> open span keys (fallback matching)
        self._last_agent: Optional[str] = None
        if _Base is not object:
            super().__init__()

    # ------------------------------------------------------------------ helpers
    def _trace(self, event) -> Optional[ManualTrace]:
        for ref in (getattr(event, "parent_event_id", None), getattr(event, "started_event_id", None),
                    getattr(event, "triggered_by_event_id", None)):
            if ref and ref in self._event_trace:
                return self._event_trace[ref]
        return self.active

    def _remember(self, event, t: Optional[ManualTrace]) -> None:
        if t is not None and getattr(event, "event_id", None):
            self._event_trace[event.event_id] = t

    def _agent_span(self, event) -> Optional[str]:
        return self._agent_key.get(f"{getattr(event, 'agent_id', None)}|{getattr(event, 'task_id', None)}") or \
            next((k for a, k in reversed(list(self._agent_key.items()))
                  if a.startswith(f"{getattr(event, 'agent_id', None)}|")), None)

    # ------------------------------------------------------------------ handlers (public so tests can drive them)
    def crew_started(self, event) -> None:
        with self._lock:
            t = ManualTrace(event.crew_name or "Crew", input=to_jsonable(event.inputs))
            self.active, self._last_agent = t, None
            self.trace_ids.append(t.trace_id)
            self._remember(event, t)

    def crew_finished(self, event, error: Optional[str] = None) -> None:
        with self._lock:
            t = self._trace(event)
            if t is None:
                return
            if getattr(event, "total_tokens", None):
                t.root.set("tdn.crew.total_tokens", event.total_tokens)
            t.finish(output=_text(getattr(event, "output", None)) if not error else None, error=error)
            if t is self.active:
                self.active = None

    def agent_started(self, event) -> None:
        with self._lock:
            t = self._trace(event)
            if t is None:
                return
            role = event.agent_role or "agent"
            if self._last_agent and self._last_agent != role:
                key = f"handoff:{event.event_id}"
                t.start(key, f"handoff {self._last_agent} -> {role}", "handoff",
                        attrs={"tdn.handoff.from": self._last_agent, "tdn.handoff.to": role})
                t.end(key)
            key = f"agent:{event.event_id}"
            t.start(key, f"invoke_agent {role}", "agent", input=getattr(event, "task_prompt", None),
                    attrs={"gen_ai.agent.name": role, "tdn.crew.task": event.task_name})
            tools = getattr(event, "tools", None) or []
            if tools and not t.root.attributes.get("tdn.tools.available"):
                t.declare_tools([{"name": getattr(x, "name", str(x)), "description": getattr(x, "description", None),
                                  "schema": (getattr(getattr(x, "args_schema", None), "model_json_schema", lambda: None)())}
                                 for x in tools])
            self._agent_key[f"{event.agent_id}|{event.task_id}"] = key
            self._last_agent = role
            self._remember(event, t)

    def agent_finished(self, event, error: Optional[str] = None) -> None:
        with self._lock:
            t = self._trace(event)
            key = self._agent_span(event)
            if t is not None and key:
                t.end(key, output=_text(getattr(event, "output", None)) if not error else None, error=error)

    def llm_started(self, event) -> None:
        with self._lock:
            t = self._trace(event)
            if t is None:
                return
            t.start(f"llm:{event.call_id}", f"chat {event.model}", "llm", parent=self._agent_span(event),
                    input=to_jsonable(event.messages),
                    attrs={"gen_ai.request.model": event.model, "gen_ai.request.temperature": event.temperature})
            self._remember(event, t)

    def llm_finished(self, event, error: Optional[str] = None) -> None:
        with self._lock:
            t = self._trace(event)
            if t is None:
                return
            usage = getattr(event, "usage", None) or {}
            t.end(f"llm:{event.call_id}", output=None if error else {"text": to_jsonable(getattr(event, "response", None))},
                  error=error, **{"gen_ai.usage.input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
                                  "gen_ai.usage.output_tokens": usage.get("completion_tokens", usage.get("output_tokens"))})

    def tool_started(self, event) -> None:
        with self._lock:
            t = self._trace(event)
            if t is None:
                return
            key = f"tool:{event.event_id}"
            t.start(key, f"execute_tool {event.tool_name}", "tool", parent=self._agent_span(event),
                    input=maybe_json(to_jsonable(event.tool_args)),
                    attrs={"gen_ai.tool.name": event.tool_name, "tdn.crew.run_attempts": event.run_attempts})
            self._tool_open.setdefault(f"{event.agent_id}|{event.tool_name}", []).append(key)
            self._remember(event, t)

    def tool_finished(self, event, error: Optional[str] = None) -> None:
        with self._lock:
            t = self._trace(event)
            if t is None:
                return
            key = f"tool:{event.started_event_id}" if getattr(event, "started_event_id", None) and \
                t.has(f"tool:{event.started_event_id}") else None
            stack = self._tool_open.get(f"{event.agent_id}|{event.tool_name}", [])
            if key is None and stack:
                key = stack[-1]
            if key in stack:
                stack.remove(key)
            if key:
                t.end(key, output=None if error else maybe_json(to_jsonable(getattr(event, "output", None))),
                      error=error, **{"tdn.crew.from_cache": getattr(event, "from_cache", None)})

    # ------------------------------------------------------------------ CrewAI wiring
    def setup_listeners(self, bus) -> None:
        import crewai.events as ev

        def on(cls, fn):
            bus.on(cls)(lambda source, event: fn(event))

        on(ev.CrewKickoffStartedEvent, self.crew_started)
        on(ev.CrewKickoffCompletedEvent, self.crew_finished)
        on(ev.CrewKickoffFailedEvent, lambda e: self.crew_finished(e, error=str(e.error)))
        on(ev.AgentExecutionStartedEvent, self.agent_started)
        on(ev.AgentExecutionCompletedEvent, self.agent_finished)
        on(ev.AgentExecutionErrorEvent, lambda e: self.agent_finished(e, error=str(e.error)))
        on(ev.LLMCallStartedEvent, self.llm_started)
        on(ev.LLMCallCompletedEvent, self.llm_finished)
        on(ev.LLMCallFailedEvent, lambda e: self.llm_finished(e, error=str(e.error)))
        on(ev.ToolUsageStartedEvent, self.tool_started)
        on(ev.ToolUsageFinishedEvent, self.tool_finished)
        on(ev.ToolUsageErrorEvent, lambda e: self.tool_finished(e, error=str(e.error)))


def instrument() -> TracedownListener:
    """Create the listener; CrewAI registers it on its global event bus."""
    return TracedownListener()
