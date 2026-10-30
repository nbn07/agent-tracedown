"""Semantic Kernel (Python).

    from agent_tracedown.integrations import semantic_kernel as tdn_sk
    tdn_sk.instrument(kernel)
    with tdn.run("TravelAgent", input=question):
        await kernel.invoke(...)            # or chat completion with auto function calling

Adds a function-invocation filter: every kernel function call becomes a span inside
the current ``tdn.run`` (native functions as tools, prompt functions as LLM calls).
Functions the model calls through auto function calling are captured the same way.
"""
from __future__ import annotations

from typing import Any

from .. import tracer
from . import to_jsonable


def _value(result: Any) -> Any:
    if result is None:
        return None
    v = getattr(result, "value", result)
    if isinstance(v, list) and v and hasattr(v[0], "content"):
        v = v[0].content
    return to_jsonable(v)


async def _filter(context, next):  # noqa: A002 - SK's filter signature
    fn = context.function
    meta = getattr(fn, "metadata", None)
    plugin = getattr(meta, "plugin_name", None) or getattr(fn, "plugin_name", None)
    name = f"{plugin}.{fn.name}" if plugin else fn.name
    is_prompt = bool(getattr(meta, "is_prompt", False))
    kind = "llm" if is_prompt else "tool"
    span_name = f"chat {name}" if is_prompt else f"execute_tool {name}"
    attrs = {"gen_ai.tool.name": name} if not is_prompt else {"gen_ai.request.model": name}
    state = tracer._run.get()
    with tracer._span_ctx(span_name, kind, **attrs) as span:
        if span is not None and state is not None:
            args = {k: to_jsonable(v) for k, v in dict(context.arguments or {}).items()}
            span.input_digest = tracer._put_blob(state, args)
        await next(context)
        if span is not None and state is not None:
            span.output_digest = tracer._put_blob(state, _value(context.result))
            usage = (getattr(context.result, "metadata", None) or {}).get("usage")
            if usage is not None:
                span.set("gen_ai.usage.input_tokens", getattr(usage, "prompt_tokens", None))
                span.set("gen_ai.usage.output_tokens", getattr(usage, "completion_tokens", None))


def instrument(kernel: Any) -> Any:
    from semantic_kernel.filters import FilterTypes
    kernel.add_filter(FilterTypes.FUNCTION_INVOCATION, _filter)
    return kernel
