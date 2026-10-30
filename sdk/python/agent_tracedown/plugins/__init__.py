"""Plugin contract and registry.

A plugin wraps a library so its calls emit TDN spans. Because every call goes
through ``tdn.llm_call`` / ``tdn.tool_call``, replay interception works for
plugin-instrumented code with no extra effort.
"""
from __future__ import annotations

from typing import Any, Dict, List

_registry: Dict[str, "Plugin"] = {}


class Plugin:
    name: str = "base"
    kind: str = "provider"  # provider | framework

    def instrument(self, target: Any) -> Any:  # pragma: no cover - interface
        """Patch ``target`` (a client or app) in place and return it."""
        raise NotImplementedError

    def uninstrument(self, target: Any) -> None:  # pragma: no cover - interface
        pass


def register(plugin: Plugin) -> Plugin:
    _registry[plugin.name] = plugin
    return plugin


def get(name: str) -> Plugin:
    try:
        return _registry[name]
    except KeyError:
        raise KeyError(f"no TDN plugin named {name!r}; registered: {sorted(_registry)}") from None


def available() -> List[str]:
    return sorted(_registry)


def instrument(name: str, target: Any) -> Any:
    """``tdn.plugins.instrument("openai", client)``."""
    return get(name).instrument(target)


from . import anthropic_plugin, openai_plugin  # noqa: E402,F401  (self-register)
