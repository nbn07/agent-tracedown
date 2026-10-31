"""Pydantic AI.

    from agent_tracedown.integrations import pydantic_ai as tdn_pai

    result = await tdn_pai.run(agent, "Refund order 42")      # or tdn_pai.run_sync(...)

Failed runs are recorded too: messages are captured with ``capture_run_messages()``
before the exception is re-raised. To record a run you already have:
``tdn_pai.trace_messages(result.all_messages(), name=..., output=result.output)``.

Each ``ModelResponse`` becomes an LLM span (model, provider, token usage, and cost when
Pydantic AI can price the model); each ``ToolCallPart`` opens a tool span closed by
its ``ToolReturnPart``. Returns whose ``outcome`` is not ``success`` (failed, denied,
retried, interrupted) are marked as failed steps, so a tool the model kept calling
with bad arguments shows up where it happened. Pydantic AI also emits OpenTelemetry
GenAI spans (``Agent.instrument_all()``); send those to ``/v1/traces`` if you prefer.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from ..tracer import ManualTrace, _put_blob
from . import maybe_json, to_jsonable


def _args(part: Any) -> Any:
    try:
        return part.args_as_dict()
    except Exception:  # noqa: BLE001
        return maybe_json(getattr(part, "args", None))


def _cost(resp: Any) -> Optional[float]:
    try:
        c = resp.cost()
        total = getattr(c, "total_price", None)
        return float(total) if total is not None else None
    except Exception:  # noqa: BLE001  (unknown model: no price)
        return None


def trace_messages(messages: Iterable[Any], name: str = "PydanticAgent", prompt: Any = None,
                   output: Any = None, error: Optional[str] = None, tools: Optional[list] = None) -> str:
    """Record a Pydantic AI message history as one run. Returns the trace id."""
    t = ManualTrace(name, input=to_jsonable(prompt) if prompt is not None else None)
    if tools:
        t.declare_tools(tools)
    agent_key = None  # steps hang off the run's root span, which already names the agent
    n, last_text, total_cost = 0, None, 0.0
    for msg in messages:
        kind = type(msg).__name__
        if kind == "ModelRequest":
            for part in msg.parts:
                p = type(part).__name__
                if p == "UserPromptPart" and t.root.input_digest is None:
                    t.root.input_digest = _put_blob(t.state, to_jsonable(part.content))
                elif p in ("ToolReturnPart", "NativeToolReturnPart") or p.endswith("ReturnPart"):
                    key = f"tool:{part.tool_call_id}"
                    if not t.has(key):  # a return with no call we saw: record it as a step anyway
                        t.start(key, f"execute_tool {part.tool_name}", "tool", parent=agent_key,
                                attrs={"gen_ai.tool.name": part.tool_name})
                    outcome = getattr(part, "outcome", "success")
                    if outcome != "success":
                        t.end(key, error=f"{outcome}: {str(part.content)[:480]}", **{"tdn.tool.outcome": outcome})
                    else:
                        t.end(key, output=to_jsonable(part.content))
                elif p == "RetryPromptPart":  # older histories
                    key = f"tool:{part.tool_call_id}"
                    if part.tool_name and t.has(key):
                        t.end(key, error=f"retried: {str(part.content)[:480]}", **{"tdn.tool.outcome": "retried"})
        elif kind == "ModelResponse":
            n += 1
            model = msg.model_name or "model"
            texts, calls = [], []
            for part in msg.parts:
                p = type(part).__name__
                if p == "TextPart":
                    texts.append(part.content)
                elif p.endswith("CallPart"):
                    calls.append({"name": part.tool_name, "arguments": _args(part)})
            usage = msg.usage
            cost = _cost(msg)
            if cost:
                total_cost += cost
            key = f"llm:{n}"
            t.start(key, f"chat {model}", "llm", parent=agent_key,
                    attrs={"gen_ai.request.model": model, "gen_ai.provider.name": msg.provider_name})
            out = {"text": "\n".join(texts)}
            if calls:
                out["tool_calls"] = calls
            t.end(key, output=out, **{"gen_ai.usage.input_tokens": getattr(usage, "input_tokens", None),
                                      "gen_ai.usage.output_tokens": getattr(usage, "output_tokens", None),
                                      "tdn.llm.cost_usd": cost})
            for part in msg.parts:
                p = type(part).__name__
                if p.endswith("CallPart"):
                    t.start(f"tool:{part.tool_call_id}", f"execute_tool {part.tool_name}", "tool", parent=agent_key,
                            input=_args(part),
                            attrs={"gen_ai.tool.name": part.tool_name, "gen_ai.tool.call.id": part.tool_call_id,
                                   "tdn.tool.kind": getattr(part, "tool_kind", None),
                                   "tdn.tool.server_side": True if p.startswith("Native") else None})
            if texts:
                last_text = "\n".join(texts)
    if error:  # the run raised: a tool still waiting for its result is where it stopped
        for key, span in t.spans.items():
            if key.startswith("tool:") and span.end_time is None:
                t.end(key, error=error)
    if total_cost:
        t.root.set("tdn.run.cost_usd", round(total_cost, 8))
    if output is None:
        output = last_text
    return t.finish(output=to_jsonable(output) if output is not None else None, error=error)


def _agent_name(agent: Any, name: Optional[str]) -> str:
    return name or getattr(agent, "name", None) or "PydanticAgent"


def _tools(agent: Any) -> Optional[list]:
    ts = getattr(getattr(agent, "_function_toolset", None), "tools", None)
    if not isinstance(ts, dict):
        return None
    return [{"name": n, "description": getattr(tool, "description", None) or ""} for n, tool in ts.items()]


async def run(agent: Any, user_prompt: Any = None, *, name: Optional[str] = None, **kwargs: Any) -> Any:
    """``await agent.run(user_prompt, **kwargs)``, recorded as one run."""
    from pydantic_ai import capture_run_messages
    label = _agent_name(agent, name)
    with capture_run_messages() as messages:
        try:
            result = await agent.run(user_prompt, **kwargs)
        except Exception as exc:
            trace_messages(messages, label, user_prompt, error=f"{type(exc).__name__}: {exc}", tools=_tools(agent))
            raise
    trace_messages(result.all_messages(), label, user_prompt, output=result.output, tools=_tools(agent))
    return result


def run_sync(agent: Any, user_prompt: Any = None, *, name: Optional[str] = None, **kwargs: Any) -> Any:
    """``agent.run_sync(user_prompt, **kwargs)``, recorded as one run."""
    from pydantic_ai import capture_run_messages
    label = _agent_name(agent, name)
    with capture_run_messages() as messages:
        try:
            result = agent.run_sync(user_prompt, **kwargs)
        except Exception as exc:
            trace_messages(messages, label, user_prompt, error=f"{type(exc).__name__}: {exc}", tools=_tools(agent))
            raise
    trace_messages(result.all_messages(), label, user_prompt, output=result.output, tools=_tools(agent))
    return result
