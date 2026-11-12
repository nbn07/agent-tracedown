"""smolagents (Hugging Face).

    from agent_tracedown.integrations import smolagents as tdn_smol

    tdn_smol.instrument(agent)     # after that, agent.run(...) is recorded as usual

The agent's ``run`` is wrapped so each run opens a Tracedown run, and a step
callback registered for smolagents' ``ActionStep`` / ``PlanningStep`` records the
steps: one LLM span per step (the model, token usage from the step, the model's
output message) and one tool span per tool call (name, arguments, and the step's
output or error). A step whose tool call failed carries no ``tool_calls`` in
smolagents, so the intended call is recovered from the step's model output
message. Code-agent steps record their code action as a ``code_execution`` tool
span. When ``run`` raises, the run is finished with the error and the exception
re-raised; when the agent stops on a step error (max steps reached), the run is
finished with that error too.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..tracer import ManualTrace
from . import maybe_json, to_jsonable


def _declared_tools(agent: Any) -> List[Dict[str, Any]]:
    tools = getattr(agent, "tools", None)
    if isinstance(tools, dict):
        tools = list(tools.values())
    if not isinstance(tools, (list, tuple)):
        return []
    out = []
    for t in tools:
        name = getattr(t, "name", None)
        if not name:
            continue
        props = {}
        for arg, spec in (getattr(t, "inputs", None) or {}).items():
            props[arg] = {"type": spec.get("type")} if isinstance(spec, dict) and spec.get("type") else {}
        out.append({"name": name, "description": getattr(t, "description", None) or "",
                    "schema": {"type": "object", "properties": props} if props else None})
    return out


def _intended_calls(step: Any) -> List[Dict[str, Any]]:
    """The tool calls a step meant to make: the step's own list, or — for a step
    whose call failed, which smolagents records with an empty list — the calls in
    the step's model output message."""
    calls = []
    for tc in getattr(step, "tool_calls", None) or []:
        calls.append({"name": getattr(tc, "name", None), "arguments": getattr(tc, "arguments", None),
                      "id": getattr(tc, "id", None)})
    if calls:
        return calls
    msg = getattr(step, "model_output_message", None)
    for tc in getattr(msg, "tool_calls", None) or []:
        fn = getattr(tc, "function", None) or {}
        name = getattr(fn, "name", None) or (fn.get("name") if isinstance(fn, dict) else None)
        args = getattr(fn, "arguments", None) or (fn.get("arguments") if isinstance(fn, dict) else None)
        calls.append({"name": name, "arguments": maybe_json(args), "id": getattr(tc, "id", None)})
    return calls


class SmolagentsRecorder:
    """Records one agent's runs. Returned by :func:`instrument`."""

    def __init__(self, agent: Any, name: Optional[str] = None) -> None:
        self.agent = agent
        self.name = name or getattr(agent, "name", None) or type(agent).__name__
        self.trace: Optional[ManualTrace] = None
        self.trace_ids: List[str] = []
        self._last_error: Optional[str] = None
        self._seq = 0

    # ------------------------------------------------------------------ steps
    def step_callback(self, step: Any, agent: Any = None) -> None:
        try:
            trace = self.trace
            if trace is None:
                return
            self._seq += 1
            n = getattr(step, "step_number", None) or f"p{self._seq}"
            error = getattr(step, "error", None)
            error_text = f"{type(error).__name__}: {error}" if error is not None else None
            self._last_error = error_text
            model = getattr(self.agent, "model", None)
            model_name = getattr(model, "model_id", None) or type(model).__name__ if model else "model"
            usage = getattr(step, "token_usage", None)
            msg = getattr(step, "model_output_message", None)
            text = getattr(msg, "content", None) or getattr(step, "model_output", None)
            llm_key = f"step:{n}:llm"
            trace.start(llm_key, f"chat {model_name}", "llm",
                        input=to_jsonable(getattr(step, "model_input_messages", None)),
                        attrs={"gen_ai.request.model": model_name})
            calls = _intended_calls(step)
            trace.end(llm_key, output={"text": text} if text else None,
                      **{"gen_ai.usage.input_tokens": getattr(usage, "input_tokens", None),
                         "gen_ai.usage.output_tokens": getattr(usage, "output_tokens", None)})
            output = getattr(step, "action_output", None)
            if output is None:
                output = getattr(step, "observations", None)
            for i, call in enumerate(calls):
                if not call["name"]:
                    continue
                key = f"step:{n}:tool:{call['id'] or i}"
                trace.start(key, f"execute_tool {call['name']}", "tool",
                            input=to_jsonable(call["arguments"]),
                            attrs={"gen_ai.tool.name": call["name"]})
                if error_text is not None and len(calls) == 1:
                    trace.end(key, error=error_text)
                else:
                    trace.end(key, output=to_jsonable(output) if output is not None else None,
                              error=error_text if error_text is not None and i == 0 else None)
            code = getattr(step, "code_action", None)
            if code and not calls:
                key = f"step:{n}:tool:code"
                trace.start(key, "execute_tool code_execution", "tool",
                            input={"code": code}, attrs={"gen_ai.tool.name": "code_execution"})
                trace.end(key, output=to_jsonable(output) if output is not None else None, error=error_text)
            if getattr(step, "is_final_answer", False):
                self._last_error = None
        except Exception:  # noqa: BLE001 - tracing must never break the agent
            pass

    # ------------------------------------------------------------------ runs
    def _wrap(self) -> None:
        agent = self.agent
        original = agent.run

        def run(task: Any = None, *args: Any, **kwargs: Any) -> Any:
            trace = ManualTrace(self.name, input=to_jsonable(task))
            tools = _declared_tools(agent)
            if tools:
                trace.declare_tools(tools)
            self.trace = trace
            self.trace_ids.append(trace.trace_id)
            self._last_error = None
            try:
                result = original(task, *args, **kwargs) if task is not None else original(*args, **kwargs)
            except Exception as exc:
                self.trace = None
                trace.finish(error=f"{type(exc).__name__}: {exc}")
                raise
            self.trace = None
            if self._last_error is not None:  # the agent stopped on a step error (e.g. max steps)
                trace.finish(error=self._last_error)
            else:
                trace.finish(output=to_jsonable(result))
            return result

        agent.run = run


def instrument(agent: Any, name: Optional[str] = None) -> SmolagentsRecorder:
    """Record a smolagents agent's runs: wraps ``agent.run`` and registers a step
    callback for its action and planning steps. Instrumenting twice is a no-op."""
    from smolagents.memory import ActionStep, PlanningStep
    existing = getattr(agent, "_tdn_recorder", None)
    if existing is not None:
        return existing
    recorder = SmolagentsRecorder(agent, name)
    agent.step_callbacks.register(ActionStep, recorder.step_callback)
    agent.step_callbacks.register(PlanningStep, recorder.step_callback)
    recorder._wrap()
    agent._tdn_recorder = recorder
    return recorder
