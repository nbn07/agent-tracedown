"""DSPy.

    from agent_tracedown.integrations import dspy as tdn_dspy

    tdn_dspy.instrument()          # after that, every DSPy program run is recorded

    # or scope it yourself:
    with dspy.context(callbacks=[tdn_dspy.callback()]):
        answer = program(question="...")

DSPy reports module, LM and tool calls through its callback interface
(``dspy.utils.callback.BaseCallback``); this handler turns them into one run per
top-level module call. Modules become agent spans, LM calls become LLM spans (model
name, and token usage where the LM records it in its history), and tool calls become
tool spans with their name and arguments. DSPy callbacks carry no parent id, so the
handler keeps a stack of the calls open on each thread; events on other threads
(async programs) nest by the same rule. A call that raises closes its span with the
error, and a failing top-level module finishes the run as failed.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

from ..tracer import ManualTrace
from . import to_jsonable

try:  # isinstance checks and the documented base class, when DSPy is installed
    from dspy.utils.callback import BaseCallback as _Base
except ImportError:  # pragma: no cover - framework not installed
    _Base = object  # type: ignore[assignment,misc]


def _module_tools(instance: Any) -> List[Dict[str, Any]]:
    """The tools a module carries (a ReAct program's, usually), as declared tools."""
    tools = getattr(instance, "tools", None)
    if isinstance(tools, dict):
        tools = list(tools.values())
    if not isinstance(tools, (list, tuple)):
        return []
    json_types = {str: "string", int: "integer", float: "number", bool: "boolean",
                  list: "array", dict: "object"}
    out = []
    for t in tools:
        name = getattr(t, "name", None) or getattr(t, "__name__", None)
        if not name:
            continue
        props = {}
        for arg, spec in (getattr(t, "args", None) or {}).items():
            if isinstance(spec, dict) and isinstance(spec.get("type"), str):
                props[arg] = {"type": spec["type"]}          # DSPy 3: {'type': 'string', ...}
            elif isinstance(spec, type):
                props[arg] = {"type": json_types.get(spec, "string")}
            elif isinstance(spec, str) and spec in json_types.values():
                props[arg] = {"type": spec}
            else:
                props[arg] = {}
        out.append({"name": name, "description": getattr(t, "desc", None) or "",
                    "schema": {"type": "object", "properties": props} if props else None})
    return out


def _lm_usage(instance: Any, outputs: Any = None) -> Tuple[Optional[int], Optional[int], Optional[float]]:
    """Token usage and cost of an LM call: from the response object when it carries
    usage (DSPy 3's LM responses do), else from the LM's history."""
    def as_int(v: Any) -> Optional[int]:
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    usage = getattr(outputs, "usage", None)
    if usage is not None:
        tin = as_int(getattr(usage, "input_tokens", None) or getattr(usage, "prompt_tokens", None))
        tout = as_int(getattr(usage, "output_tokens", None) or getattr(usage, "completion_tokens", None))
        if tin is not None or tout is not None:
            return tin, tout, None
    history = getattr(instance, "history", None) or []
    if not history:
        return None, None, None
    last = history[-1] or {}
    usage = last.get("usage") or {}
    cost = last.get("cost")
    return (as_int(usage.get("prompt_tokens", usage.get("input_tokens"))),
            as_int(usage.get("completion_tokens", usage.get("output_tokens"))),
            float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None)


def _tool_args(instance: Any, inputs: Any) -> Any:
    """DSPy's callback wrapper reports a tool call's inputs as an
    ``{"args": (...), "kwargs": {...}}`` envelope; record the arguments themselves,
    mapping positionals onto the tool's declared argument names when it has them."""
    if isinstance(inputs, dict) and inputs and set(inputs) <= {"args", "kwargs"}:
        kwargs = inputs.get("kwargs") or {}
        args = inputs.get("args") or ()
        if args:
            names = list((getattr(instance, "args", None) or {}).keys())
            merged = dict(zip(names, args))
            merged.update(kwargs)
            return merged
        return kwargs
    return inputs


def _text_of(outputs: Any) -> Optional[str]:
    if outputs is None:
        return None
    if isinstance(outputs, str):
        return outputs
    text = getattr(outputs, "text", None)  # an LM response object (DSPy 3 / litellm shape)
    if isinstance(text, str):
        return text
    if isinstance(outputs, (list, tuple)):
        parts = [_text_of(o) for o in outputs]
        parts = [p for p in parts if p]
        return "\n".join(parts) or None
    if isinstance(outputs, dict):
        for k in ("text", "answer", "output", "content"):
            if isinstance(outputs.get(k), str):
                return outputs[k]
    return None


class TracedownCallbackHandler(_Base):  # type: ignore[misc,valid-type]
    """DSPy callback handler that records each top-level module call as one run."""

    def __init__(self, name: Optional[str] = None) -> None:
        if _Base is not object:
            super().__init__()
        self.name = name
        self._local = threading.local()
        self._open: Dict[str, Tuple[ManualTrace, Optional[str]]] = {}
        self._lm: Dict[str, Any] = {}
        self.trace_ids: List[str] = []

    # ------------------------------------------------------------------ plumbing
    def _stack(self) -> List[str]:
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = self._local.stack = []
        return stack

    def _current(self) -> Tuple[Optional[ManualTrace], Optional[str]]:
        stack = self._stack()
        if not stack:
            return None, None
        return self._open.get(stack[-1], (None, None))

    def _pop(self, call_id: str) -> None:
        stack = self._stack()
        if call_id in stack:
            stack.remove(call_id)
        self._open.pop(call_id, None)
        self._lm.pop(call_id, None)

    # ------------------------------------------------------------------ modules
    def on_module_start(self, call_id: str, instance: Any, inputs: Dict[str, Any]) -> None:
        try:
            stack = self._stack()
            if not stack:  # the outermost module: its call is the run
                trace = ManualTrace(self.name or type(instance).__name__,
                                    input=to_jsonable(inputs))
                self.trace_ids.append(trace.trace_id)
                self._open[call_id] = (trace, None)
                stack.append(call_id)
                tools = _module_tools(instance)
                if tools:
                    trace.declare_tools(tools)
                return
            trace, parent = self._current()
            if trace is None:
                return
            trace.start(call_id, f"invoke_agent {type(instance).__name__}", "agent",
                        parent=parent, input=to_jsonable(inputs))
            self._open[call_id] = (trace, call_id)
            stack.append(call_id)
        except Exception:  # noqa: BLE001 - tracing must never break the program
            pass

    def on_module_end(self, call_id: str, outputs: Any = None,
                      exception: Optional[BaseException] = None) -> None:
        try:
            entry = self._open.get(call_id)
            if entry is None:
                return
            trace, key = entry
            error = f"{type(exception).__name__}: {exception}" if exception is not None else None
            self._pop(call_id)
            if key is None:  # the run itself
                trace.finish(output=to_jsonable(outputs) if error is None else None, error=error)
            else:
                trace.end(call_id, output=to_jsonable(outputs) if error is None else None, error=error)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ LM calls
    def on_lm_start(self, call_id: str, instance: Any, inputs: Dict[str, Any]) -> None:
        try:
            trace, parent = self._current()
            if trace is None:
                return
            model = getattr(instance, "model", None) or "model"
            trace.start(call_id, f"chat {model}", "llm", parent=parent, input=to_jsonable(inputs),
                        attrs={"gen_ai.request.model": model})
            self._open[call_id] = (trace, call_id)
            self._lm[call_id] = instance
            self._stack().append(call_id)
        except Exception:  # noqa: BLE001
            pass

    def on_lm_end(self, call_id: str, outputs: Any = None,
                  exception: Optional[BaseException] = None) -> None:
        try:
            entry = self._open.get(call_id)
            if entry is None:
                return
            trace, _ = entry
            instance = self._lm.get(call_id)
            self._pop(call_id)
            if exception is not None:
                trace.end(call_id, error=f"{type(exception).__name__}: {exception}")
                return
            tin, tout, cost = _lm_usage(instance, outputs)
            trace.end(call_id, output={"text": _text_of(outputs)},
                      **{"gen_ai.usage.input_tokens": tin, "gen_ai.usage.output_tokens": tout,
                         "tdn.llm.cost_usd": cost})
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ tools
    def on_tool_start(self, call_id: str, instance: Any, inputs: Dict[str, Any]) -> None:
        try:
            trace, parent = self._current()
            if trace is None:
                return
            name = getattr(instance, "name", None) or getattr(instance, "__name__", None) or "tool"
            trace.start(call_id, f"execute_tool {name}", "tool", parent=parent,
                        input=to_jsonable(_tool_args(instance, inputs)), attrs={"gen_ai.tool.name": name})
            self._open[call_id] = (trace, call_id)
            self._stack().append(call_id)
        except Exception:  # noqa: BLE001
            pass

    def on_tool_end(self, call_id: str, outputs: Any = None,
                    exception: Optional[BaseException] = None) -> None:
        try:
            entry = self._open.get(call_id)
            if entry is None:
                return
            trace, _ = entry
            self._pop(call_id)
            if exception is not None:
                trace.end(call_id, error=f"{type(exception).__name__}: {exception}")
            else:
                trace.end(call_id, output=to_jsonable(outputs))
        except Exception:  # noqa: BLE001
            pass


def callback(name: Optional[str] = None) -> TracedownCallbackHandler:
    """A handler to pass to ``dspy.context(callbacks=[...])`` or ``dspy.configure``."""
    return TracedownCallbackHandler(name)


def instrument(name: Optional[str] = None) -> TracedownCallbackHandler:
    """Record every DSPy program run from now on (adds the handler to DSPy's
    global callbacks; an existing Tracedown handler is not added twice)."""
    import dspy
    handler = TracedownCallbackHandler(name)
    callbacks = list(getattr(dspy.settings, "callbacks", None) or [])
    if not any(isinstance(c, TracedownCallbackHandler) for c in callbacks):
        dspy.configure(callbacks=callbacks + [handler])
    return handler
