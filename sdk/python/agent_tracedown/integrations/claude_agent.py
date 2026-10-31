"""Claude Agent SDK (``claude-agent-sdk``).

    from claude_agent_sdk import query, ClaudeAgentOptions
    from agent_tracedown.integrations import claude_agent as tdn_claude

    async for msg in tdn_claude.trace_stream(query(prompt=p, options=opts), name="CodingAgent", prompt=p):
        ...                                  # messages pass through unchanged

Works the same with ``ClaudeSDKClient.receive_response()``. After the fact:
``tdn_claude.trace_messages(messages, name=..., prompt=...)``.

Each ``AssistantMessage`` becomes an LLM span (model, token usage); each
``ToolUseBlock`` opens a tool span that the matching ``ToolResultBlock`` closes, with
``is_error`` results marked as failures. Subagents started through the ``Task`` /
``Agent`` tool become agent spans, and messages carrying their ``parent_tool_use_id``
nest under them. The ``ResultMessage`` sets the outcome, the final answer and the cost
(``tdn.run.cost_usd``); the init ``SystemMessage`` declares the available tools.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Iterable, Optional

from ..tracer import ManualTrace, _put_blob
from . import maybe_json, to_jsonable

SUBAGENT_TOOLS = ("Task", "Agent")


def _usage(u: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(u, dict):
        return {}
    inp = u.get("input_tokens")
    if inp is not None:  # cached prompt tokens are input tokens too
        inp += (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
    return {"gen_ai.usage.input_tokens": inp, "gen_ai.usage.output_tokens": u.get("output_tokens")}


def _result_payload(content: Any) -> Any:
    if isinstance(content, list):  # [{"type": "text", "text": ...}, ...]
        texts = [c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        if texts and len(texts) == len(content):
            return maybe_json("\n".join(texts))
        return to_jsonable(content)
    return maybe_json(content)


class _Builder:
    def __init__(self, name: str, prompt: Any = None) -> None:
        self.t = ManualTrace(name, input=prompt if isinstance(prompt, (str, dict, list)) else None)
        self.n = 0
        self.final: Any = None
        self.error: Optional[str] = None
        self.subagents: Dict[str, str] = {}  # tool_use_id -> agent span key

    def _parent(self, msg: Any) -> Optional[str]:
        pid = getattr(msg, "parent_tool_use_id", None)
        return self.subagents.get(pid) if pid else None

    def feed(self, msg: Any) -> None:
        kind = type(msg).__name__
        if kind == "SystemMessage":
            data = getattr(msg, "data", None) or {}
            if getattr(msg, "subtype", None) == "init":
                if data.get("tools"):
                    self.t.declare_tools([{"name": n} for n in data["tools"] if isinstance(n, str)])
                if data.get("model"):
                    self.t.root.set("gen_ai.request.model", data["model"])
                if data.get("session_id"):
                    self.t.root.set("tdn.claude.session_id", data["session_id"])
        elif kind == "UserMessage":
            content = msg.content
            if isinstance(content, str):
                if self.t.root.input_digest is None and not getattr(msg, "parent_tool_use_id", None):
                    self.t.root.input_digest = _put_blob(self.t.state, content)
                return
            for block in content:
                if type(block).__name__ != "ToolResultBlock":
                    continue
                key = f"tool:{block.tool_use_id}"
                agent_key = self.subagents.get(block.tool_use_id)
                err = str(block.content)[:500] if block.is_error else None
                out = None if block.is_error else _result_payload(block.content)
                if agent_key:
                    self.t.end(agent_key, output=out, error=err)
                else:
                    self.t.end(key, output=out, error=err)
        elif kind == "AssistantMessage":
            parent = self._parent(msg)
            self.n += 1
            llm = f"llm:{self.n}"
            texts, calls = [], []
            for block in msg.content:
                b = type(block).__name__
                if b == "TextBlock":
                    texts.append(block.text)
                elif b in ("ToolUseBlock", "ServerToolUseBlock"):
                    calls.append({"name": block.name, "arguments": to_jsonable(block.input)})
            self.t.start(llm, f"chat {msg.model}", "llm", parent=parent,
                         attrs={"gen_ai.request.model": msg.model, "gen_ai.provider.name": "anthropic",
                                "gen_ai.response.finish_reasons": [msg.stop_reason] if getattr(msg, "stop_reason", None) else None})
            output: Dict[str, Any] = {"text": "\n".join(texts)}
            if calls:
                output["tool_calls"] = calls
            api_error = getattr(msg, "error", None)
            self.t.end(llm, output=output, error=f"API error: {api_error}" if api_error else None,
                       **_usage(getattr(msg, "usage", None)))
            for block in msg.content:
                b = type(block).__name__
                if b == "ToolUseBlock" and block.name in SUBAGENT_TOOLS:
                    inp = block.input or {}
                    agent = inp.get("subagent_type") or inp.get("description") or "subagent"
                    key = f"agent:{block.id}"
                    self.t.start(key, f"invoke_agent {agent}", "agent", parent=parent, input=to_jsonable(inp),
                                 attrs={"gen_ai.agent.name": agent, "tdn.agent.role": inp.get("description")})
                    self.subagents[block.id] = key
                elif b in ("ToolUseBlock", "ServerToolUseBlock"):
                    self.t.start(f"tool:{block.id}", f"execute_tool {block.name}", "tool", parent=parent,
                                 input=to_jsonable(block.input),
                                 attrs={"gen_ai.tool.name": block.name, "gen_ai.tool.call.id": block.id,
                                        "tdn.tool.server_side": True if b == "ServerToolUseBlock" else None})
                elif b == "ServerToolResultBlock":
                    self.t.end(f"tool:{block.tool_use_id}", output=to_jsonable(block.content))
            if texts and not parent:
                self.final = "\n".join(texts)
        elif kind == "ResultMessage":
            root = self.t.root
            if msg.total_cost_usd is not None:
                root.set("tdn.run.cost_usd", float(msg.total_cost_usd))
                root.set("tdn.llm.cost_usd", float(msg.total_cost_usd))
            root.set("tdn.claude.num_turns", msg.num_turns)
            root.set("tdn.claude.stop_reason", getattr(msg, "terminal_reason", None) or msg.subtype)
            root.set("tdn.claude.session_id", msg.session_id)
            u = _usage(getattr(msg, "usage", None))
            for k, v in u.items():
                if v is not None:
                    root.set(k, v)
            if msg.result is not None:
                self.final = msg.result
            if msg.is_error:
                detail = "; ".join(getattr(msg, "errors", None) or []) or msg.result or msg.subtype
                self.error = f"{msg.subtype}: {detail}"[:500]

    def finish(self) -> str:
        return self.t.finish(output=self.final, error=self.error)


async def trace_stream(stream: AsyncIterator[Any], name: str = "ClaudeAgent", prompt: Any = None) -> AsyncIterator[Any]:
    """Wrap ``query(...)`` or ``client.receive_response()``; yields every message unchanged."""
    b = _Builder(name, prompt)
    try:
        async for msg in stream:
            b.feed(msg)
            yield msg
    except Exception as exc:
        b.error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        b.finish()


def trace_messages(messages: Iterable[Any], name: str = "ClaudeAgent", prompt: Any = None) -> str:
    """Record messages already collected from a run. Returns the trace id."""
    b = _Builder(name, prompt)
    for m in messages:
        b.feed(m)
    return b.finish()
