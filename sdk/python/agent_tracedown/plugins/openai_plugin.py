"""OpenAI (and OpenAI-compatible: Ollama, DeepSeek, vLLM) chat completions.

    client = tdn.plugins.instrument("openai", OpenAI())

Works by wrapping ``client.chat.completions.create``. Duck-typed: the openai
package is not imported, so any object with that shape can be instrumented.
"""
from __future__ import annotations

from typing import Any

from .. import tracer
from . import Plugin, register


class OpenAIPlugin(Plugin):
    name = "openai"
    provider = "openai"

    def instrument(self, client: Any) -> Any:
        completions = client.chat.completions
        if getattr(completions, "_abx_original", None):
            return client
        original = completions.create
        provider = self.provider

        def create(*args: Any, **kwargs: Any) -> Any:
            messages = kwargs.get("messages")
            model = kwargs.get("model", "unknown")

            def call(prompt, model, temperature, **_):
                kw = dict(kwargs, messages=prompt, model=model)
                if temperature is not None:
                    kw["temperature"] = temperature
                resp = original(*args, **kw)
                return _normalize(resp)

            result = tracer.llm_call(call, prompt=messages, model=model, provider=provider,
                                     temperature=kwargs.get("temperature"), seed=kwargs.get("seed"))
            return result

        completions._abx_original = original
        completions.create = create
        return client

    def uninstrument(self, client: Any) -> None:
        completions = client.chat.completions
        if getattr(completions, "_abx_original", None):
            completions.create = completions._abx_original
            completions._abx_original = None


def _normalize(resp: Any) -> Any:
    """Turn a ChatCompletion into a JSON-safe dict: {text, tool_calls, usage, finish_reason}.

    Recording a plain dict (not the SDK object) is what makes the step replayable.
    """
    if isinstance(resp, dict):
        return resp
    choice = resp.choices[0]
    msg = choice.message
    usage = getattr(resp, "usage", None)
    return {
        "text": getattr(msg, "content", None),
        "tool_calls": [
            {"name": tc.function.name, "arguments": tc.function.arguments}
            for tc in (getattr(msg, "tool_calls", None) or [])
        ],
        "finish_reason": getattr(choice, "finish_reason", None),
        "usage": {
            "input_tokens": getattr(usage, "prompt_tokens", None),
            "output_tokens": getattr(usage, "completion_tokens", None),
        } if usage else None,
    }


register(OpenAIPlugin())
