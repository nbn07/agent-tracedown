"""Anthropic Messages API.

    client = tdn.plugins.instrument("anthropic", Anthropic())

Wraps ``client.messages.create``; duck-typed, so the anthropic package is not imported.
"""
from __future__ import annotations

from typing import Any

from .. import tracer
from . import Plugin, register


class AnthropicPlugin(Plugin):
    name = "anthropic"

    def instrument(self, client: Any) -> Any:
        messages_api = client.messages
        if getattr(messages_api, "_abx_original", None):
            return client
        original = messages_api.create

        def create(*args: Any, **kwargs: Any) -> Any:
            prompt = {"system": kwargs.get("system"), "messages": kwargs.get("messages")}
            model = kwargs.get("model", "unknown")

            def call(p, model, temperature, **_):
                kw = dict(kwargs, model=model, messages=p["messages"])
                if p.get("system") is not None:
                    kw["system"] = p["system"]
                if temperature is not None:
                    kw["temperature"] = temperature
                return _normalize(original(*args, **kw))

            return tracer.llm_call(call, prompt=prompt, model=model, provider="anthropic",
                                   temperature=kwargs.get("temperature"))

        messages_api._abx_original = original
        messages_api.create = create
        return client

    def uninstrument(self, client: Any) -> None:
        api = client.messages
        if getattr(api, "_abx_original", None):
            api.create = api._abx_original
            api._abx_original = None


def _normalize(resp: Any) -> Any:
    if isinstance(resp, dict):
        return resp
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
    tool_uses = [{"name": b.name, "arguments": b.input} for b in resp.content if getattr(b, "type", "") == "tool_use"]
    usage = getattr(resp, "usage", None)
    return {
        "text": text,
        "tool_calls": tool_uses,
        "finish_reason": getattr(resp, "stop_reason", None),
        "usage": {"input_tokens": getattr(usage, "input_tokens", None),
                  "output_tokens": getattr(usage, "output_tokens", None)} if usage else None,
    }


register(AnthropicPlugin())
