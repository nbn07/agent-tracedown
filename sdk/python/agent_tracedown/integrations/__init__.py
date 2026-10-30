"""Agent framework integrations.

Each adapter turns a framework's own callbacks or events into TDN spans, so a run in
that framework shows up with its agents, LLM calls, tools, retrievals and handoffs.

| Framework         | How to enable                                                        |
|-------------------|----------------------------------------------------------------------|
| LangChain/LangGraph | ``graph.invoke(x, config={"callbacks": [langchain.callback()]})``  |
| OpenAI Agents SDK | ``openai_agents.instrument()`` (adds a tracing processor)            |
| CrewAI            | ``crewai.instrument()`` (registers an event listener)                |
| AutoGen AgentChat | ``async for m in autogen.trace_stream(team.run_stream(task=t), ...)``|
| Semantic Kernel   | ``semantic_kernel.instrument(kernel)`` inside ``tdn.run(...)``       |
| LlamaIndex        | ``llamaindex.instrument()`` (span + event handlers)                  |

Adapters import their framework lazily: installing ``agent-tracedown`` never pulls in
a framework. Replay of adapter-traced runs works for steps that go through an
instrumented model client (``tdn.plugins.instrument("openai", client)``); framework
callbacks can observe but not substitute outputs.
"""
from __future__ import annotations

import json
from typing import Any


def to_jsonable(value: Any, depth: int = 0) -> Any:
    """Best-effort conversion of framework objects (pydantic models, dataclasses,
    messages) into JSON-safe values for payload capture."""
    if depth > 6:
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): to_jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v, depth + 1) for v in value]
    for attr in ("model_dump", "dict", "to_dict"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return to_jsonable(fn(), depth + 1)
            except Exception:  # noqa: BLE001
                pass
    if hasattr(value, "__dataclass_fields__"):
        return {k: to_jsonable(getattr(value, k, None), depth + 1) for k in value.__dataclass_fields__}
    content = getattr(value, "content", None)
    if isinstance(content, str):
        return content
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def maybe_json(text: Any) -> Any:
    if isinstance(text, str) and text[:1] in "{[":
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text
