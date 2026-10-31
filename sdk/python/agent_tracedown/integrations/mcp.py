"""MCP (Model Context Protocol) client tool calls, with the official ``mcp`` package.

    from agent_tracedown.integrations import mcp as tdn_mcp

    async with Client("http://localhost:8000/mcp") as client:   # or a ClientSession
        tdn_mcp.instrument(client, server="billing")
        with tdn.run("SupportAgent", input=question):
            tools = await client.list_tools()        # declares the tools on the run
            res = await client.call_tool("refund", {"order_id": 42})

or ``tdn_mcp.instrument_all()`` once at startup to cover every ``Client`` and
``ClientSession``.

Inside ``tdn.run(...)`` (or a framework integration's run), each ``call_tool`` becomes
a tool span with its arguments and result; ``is_error`` results and exceptions are
marked as failed steps. ``list_tools`` records the server's tools, descriptions and
input schemas on the run, which lets Tracedown check tool choice and arguments.
Outside a run nothing is recorded and calls behave exactly as before.
"""
from __future__ import annotations

import contextvars
import functools
from typing import Any, Dict, List, Optional

from ..tracer import _put_blob, _run, _span_ctx, declare_tools
from . import maybe_json, to_jsonable

_MARK = "__tdn_mcp_wrapped__"
# mcp.Client delegates to ClientSession: record only the outermost call
_inside = contextvars.ContextVar("tdn_mcp_inside", default=False)


def _content(result: Any) -> Any:
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return to_jsonable(structured)
    parts = getattr(result, "content", None) or []
    texts = [p.text for p in parts if getattr(p, "type", None) == "text" and getattr(p, "text", None) is not None]
    if texts and len(texts) == len(parts):
        return maybe_json("\n".join(texts))
    return to_jsonable(parts)


def _tool_dicts(result: Any) -> List[Dict[str, Any]]:
    return [{"name": t.name, "description": t.description or t.title or "",
             "schema": to_jsonable(getattr(t, "input_schema", None))}
            for t in (getattr(result, "tools", None) or [])]


def _wrap_call_tool(fn: Any, server: Optional[str]) -> Any:
    @functools.wraps(fn)
    async def call_tool(name: str, arguments: Optional[Dict[str, Any]] = None, *args: Any, **kwargs: Any) -> Any:
        state = _run.get()
        if state is None or _inside.get():
            return await fn(name, arguments, *args, **kwargs)
        token = _inside.set(True)
        try:
            return await _recorded(state, name, arguments, args, kwargs)
        finally:
            _inside.reset(token)

    async def _recorded(state: Any, name: str, arguments: Any, args: Any, kwargs: Any) -> Any:
        with _span_ctx(f"execute_tool {name}", "tool", **{"gen_ai.tool.name": name, "tdn.mcp.server": server,
                                                            "tdn.tool.protocol": "mcp"}) as span:
            if span is not None:
                span.input_digest = _put_blob(state, to_jsonable(arguments or {}))
            result = await fn(name, arguments, *args, **kwargs)
            if span is not None:
                out = _content(result)
                if getattr(result, "is_error", False):
                    span.error(str(out)[:500] or "MCP tool returned isError")
                else:
                    span.output_digest = _put_blob(state, out)
            return result

    setattr(call_tool, _MARK, True)
    return call_tool


def _wrap_list_tools(fn: Any, server: Optional[str]) -> Any:
    @functools.wraps(fn)
    async def list_tools(*args: Any, **kwargs: Any) -> Any:
        result = await fn(*args, **kwargs)
        state = _run.get()
        if state is not None:
            root = next((s for s in state.spans if s.parent_span_id is None), None)
            existing = list(root.attributes.get("tdn.tools.available") or []) if root else []
            seen = {t.get("name") for t in existing}
            new = [t for t in _tool_dicts(result) if t["name"] not in seen]
            if new:
                declare_tools(existing + new)
        return result

    setattr(list_tools, _MARK, True)
    return list_tools


def instrument(client: Any, server: Optional[str] = None) -> Any:
    """Record this client's (``mcp.Client`` or ``mcp.ClientSession``) tool calls. Returns it."""
    call = getattr(client, "call_tool", None)
    if call is not None and not getattr(call, _MARK, False):
        object.__setattr__(client, "call_tool", _wrap_call_tool(call, server))
    lst = getattr(client, "list_tools", None)
    if lst is not None and not getattr(lst, _MARK, False):
        object.__setattr__(client, "list_tools", _wrap_list_tools(lst, server))
    return client


def instrument_all() -> None:
    """Patch ``mcp.Client`` and ``mcp.ClientSession`` so every client is recorded."""
    import mcp

    for cls in {getattr(mcp, "Client", None), getattr(mcp, "ClientSession", None)} - {None}:
        for attr, wrap in (("call_tool", _wrap_call_tool), ("list_tools", _wrap_list_tools)):
            orig = cls.__dict__.get(attr)
            if orig is None or getattr(orig, _MARK, False):
                continue

            def make(orig=orig, wrap=wrap):
                @functools.wraps(orig)
                async def method(self, *args: Any, **kwargs: Any) -> Any:
                    return await wrap(functools.partial(orig, self), None)(*args, **kwargs)
                setattr(method, _MARK, True)
                return method

            setattr(cls, attr, make())
