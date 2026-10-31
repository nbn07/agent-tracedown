"""Google Agent Development Kit (``google-adk``).

    from agent_tracedown.integrations import google_adk as tdn_adk

    app = App(name="support", root_agent=root_agent, plugins=[tdn_adk.TracedownPlugin()])
    runner = InMemoryRunner(app=app)          # or Runner(app=app, session_service=...)

Every ``runner.run_async(...)`` invocation becomes one run. Each agent (including
sub-agents, sequential/parallel/loop agents and agent transfers) becomes an agent span
under its parent agent; each model call becomes an LLM span with model and token usage;
each tool call becomes a tool span with its arguments and result. Model and tool errors
are recorded on the step where they happened. The plugin only observes: every callback
returns ``None``, so ADK behaves exactly as without it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..tracer import ManualTrace
from . import to_jsonable


def _text(content: Any) -> Optional[str]:
    parts = getattr(content, "parts", None) or []
    texts = [p.text for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False)]
    return "\n".join(texts) if texts else None


def _calls(content: Any) -> List[Dict[str, Any]]:
    parts = getattr(content, "parts", None) or []
    return [{"name": p.function_call.name, "arguments": to_jsonable(p.function_call.args)}
            for p in parts if getattr(p, "function_call", None)]


def _base():
    try:
        from google.adk.plugins.base_plugin import BasePlugin
    except ImportError as exc:  # pragma: no cover
        raise ImportError("google_adk integration needs `pip install google-adk`") from exc
    return BasePlugin


class _Invocation:
    def __init__(self, trace: ManualTrace) -> None:
        self.t = trace
        self.llm: Dict[str, str] = {}  # agent name -> open llm span key
        self.n = 0
        self.final: Optional[str] = None
        self.tools: Dict[str, Dict[str, Any]] = {}


def TracedownPlugin(name: str = "tracedown"):  # noqa: N802  (reads like a class at call sites)
    """Create the ADK plugin. A factory so ``google-adk`` is imported only when used."""
    BasePlugin = _base()

    class _Plugin(BasePlugin):
        def __init__(self) -> None:
            super().__init__(name=name)
            self.runs: Dict[str, _Invocation] = {}

        # ---------------------------------------------------------------- run
        async def before_run_callback(self, *, invocation_context):
            agent = invocation_context.agent
            prompt = _text(getattr(invocation_context, "user_content", None))
            t = ManualTrace(getattr(agent, "name", "ADKAgent"), input=prompt,
                            **{"tdn.adk.invocation_id": invocation_context.invocation_id,
                               "tdn.adk.session_id": getattr(getattr(invocation_context, "session", None), "id", None)})
            self.runs[invocation_context.invocation_id] = _Invocation(t)
            return None

        async def on_event_callback(self, *, invocation_context, event):
            inv = self.runs.get(invocation_context.invocation_id)
            if inv is not None and getattr(event, "content", None) is not None:
                try:
                    final = event.is_final_response()
                except Exception:  # noqa: BLE001
                    final = False
                text = _text(event.content)
                if final and text:
                    inv.final = text
            return None

        def _finish(self, invocation_id: str, error: Optional[str] = None) -> None:
            inv = self.runs.pop(invocation_id, None)
            if inv is None:
                return
            if inv.tools:
                inv.t.declare_tools(list(inv.tools.values()))
            if error:
                for key, span in inv.t.spans.items():
                    if span.end_time is None and span.kind != "agent":
                        inv.t.end(key, error=error)
            inv.t.finish(output=inv.final, error=error)

        async def after_run_callback(self, *, invocation_context):
            self._finish(invocation_context.invocation_id)
            return None

        async def on_run_error_callback(self, *, invocation_context, error):
            self._finish(invocation_context.invocation_id, f"{type(error).__name__}: {error}")
            return None

        # ---------------------------------------------------------------- agents
        def _inv(self, ctx: Any) -> Optional[_Invocation]:
            return self.runs.get(getattr(ctx, "invocation_id", None))

        async def before_agent_callback(self, *, agent, callback_context):
            inv = self._inv(callback_context)
            if inv is None:
                return None
            parent = getattr(getattr(agent, "parent_agent", None), "name", None)
            parent_key = f"agent:{parent}" if parent and inv.t.has(f"agent:{parent}") else None
            key = f"agent:{agent.name}"
            if inv.t.has(key):  # the same agent again (a loop, a transfer back): a new span
                inv.n += 1
                inv.t.spans[f"{key}#{inv.n}"] = inv.t.spans.pop(key)
            inv.t.start(key, f"invoke_agent {agent.name}", "agent", parent=parent_key,
                        attrs={"gen_ai.agent.name": agent.name, "tdn.agent.role": getattr(agent, "description", None) or None,
                               "tdn.adk.agent_type": type(agent).__name__})
            return None

        async def after_agent_callback(self, *, agent, callback_context):
            inv = self._inv(callback_context)
            if inv is not None:
                inv.t.end(f"agent:{agent.name}")
            return None

        async def on_agent_error_callback(self, *, agent, callback_context, error):
            inv = self._inv(callback_context)
            if inv is not None:
                inv.t.end(f"agent:{agent.name}", error=f"{type(error).__name__}: {error}")
            return None

        # ---------------------------------------------------------------- model
        async def before_model_callback(self, *, callback_context, llm_request):
            inv = self._inv(callback_context)
            if inv is None:
                return None
            agent = callback_context.agent_name
            inv.n += 1
            key = f"llm:{inv.n}"
            inv.llm[agent] = key
            model = getattr(llm_request, "model", None) or "model"
            contents = [{"role": c.role, "text": _text(c), "tool_calls": _calls(c) or None}
                        for c in (getattr(llm_request, "contents", None) or [])]
            inv.t.start(key, f"chat {model}", "llm", parent=f"agent:{agent}" if inv.t.has(f"agent:{agent}") else None,
                        input=contents, attrs={"gen_ai.request.model": model, "gen_ai.provider.name": "google"})
            for tname, tool in (getattr(llm_request, "tools_dict", None) or {}).items():
                inv.tools.setdefault(tname, {"name": tname, "description": getattr(tool, "description", "") or ""})
            return None

        async def after_model_callback(self, *, callback_context, llm_response):
            inv = self._inv(callback_context)
            if inv is None or getattr(llm_response, "partial", False):
                return None
            key = inv.llm.pop(callback_context.agent_name, None)
            if key is None:
                return None
            usage = getattr(llm_response, "usage_metadata", None)
            out: Dict[str, Any] = {"text": _text(llm_response.content) or ""}
            calls = _calls(llm_response.content)
            if calls:
                out["tool_calls"] = calls
            err = getattr(llm_response, "error_code", None)
            inv.t.end(key, output=out,
                      error=f"{err}: {llm_response.error_message or ''}".strip() if err else None,
                      **{"gen_ai.response.model": getattr(llm_response, "model_version", None),
                         "gen_ai.usage.input_tokens": getattr(usage, "prompt_token_count", None),
                         "gen_ai.usage.output_tokens": getattr(usage, "candidates_token_count", None)})
            return None

        async def on_model_error_callback(self, *, callback_context, llm_request, error):
            inv = self._inv(callback_context)
            key = inv.llm.pop(callback_context.agent_name, None) if inv else None
            if key:
                inv.t.end(key, error=f"{type(error).__name__}: {error}")
            return None

        # ---------------------------------------------------------------- tools
        def _tool_key(self, tool_context: Any, tool: Any) -> str:
            return f"tool:{getattr(tool_context, 'function_call_id', None) or tool.name}"

        async def before_tool_callback(self, *, tool, tool_args, tool_context):
            inv = self._inv(tool_context)
            if inv is None:
                return None
            agent = getattr(tool_context, "agent_name", None)
            inv.t.start(self._tool_key(tool_context, tool), f"execute_tool {tool.name}", "tool",
                        parent=f"agent:{agent}" if agent and inv.t.has(f"agent:{agent}") else None,
                        input=to_jsonable(tool_args),
                        attrs={"gen_ai.tool.name": tool.name,
                               "gen_ai.tool.call.id": getattr(tool_context, "function_call_id", None)})
            return None

        async def after_tool_callback(self, *, tool, tool_args, tool_context, result):
            inv = self._inv(tool_context)
            if inv is not None:
                res = to_jsonable(result)
                # ADK tools conventionally report failure as {"status": "error", "error_message": ...}
                err = None
                if isinstance(res, dict) and str(res.get("status", "")).lower() == "error":
                    err = str(res.get("error_message") or res.get("error") or "tool returned status=error")[:500]
                inv.t.end(self._tool_key(tool_context, tool), output=res, error=err)
            return None

        async def on_tool_error_callback(self, *, tool, tool_args, tool_context, error):
            inv = self._inv(tool_context)
            if inv is not None:
                inv.t.end(self._tool_key(tool_context, tool), error=f"{type(error).__name__}: {error}")
            return None

    return _Plugin()


__all__ = ["TracedownPlugin"]
