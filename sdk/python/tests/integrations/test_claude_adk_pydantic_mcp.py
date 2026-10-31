"""Claude Agent SDK (real message dataclasses), Google ADK (real runner, fake BaseLlm),
Pydantic AI (real agent, FunctionModel) and MCP (real in-process server and client).
No network. Each failing run is checked against the server's root-cause analysis."""
import asyncio
import json

import pytest

import agent_tracedown as tdn


@pytest.fixture()
def mem():
    m = tdn.MemoryExporter()
    tdn.init(project="t", exporter=m)
    return m


def ordered(mem):
    return sorted(mem.spans, key=lambda s: s["attributes"]["tdn.step"])


def root_of(mem):
    return next(s for s in mem.spans if s["parent_span_id"] is None)


def rca(mem):
    engine = pytest.importorskip("tracedown_server.rca.engine")
    return engine.analyze(mem.spans, mem.blobs)


# ----------------------------------------------------------------------------- Claude Agent SDK

def _claude_messages(failing: bool):
    from claude_agent_sdk import (AssistantMessage, ResultMessage, SystemMessage, TextBlock, ToolResultBlock,
                                  ToolUseBlock, UserMessage)

    usage = {"input_tokens": 20, "cache_read_input_tokens": 100, "output_tokens": 12}
    return [
        SystemMessage(subtype="init", data={"tools": ["Read", "Bash", "Task"], "model": "claude-sonnet-5-5",
                                            "session_id": "s1"}),
        UserMessage(content="Why do the tests fail?"),
        AssistantMessage(content=[TextBlock("Let me check with a subagent."),
                                  ToolUseBlock(id="t1", name="Task", input={"subagent_type": "test-runner",
                                                                            "description": "run tests",
                                                                            "prompt": "run pytest"})],
                         model="claude-sonnet-5-5", usage=usage),
        AssistantMessage(content=[ToolUseBlock(id="b1", name="Bash", input={"command": "pytest -q"})],
                         model="claude-haiku-5-5", parent_tool_use_id="t1", usage=usage),
        UserMessage(content=[ToolResultBlock(tool_use_id="b1", content="bash: pytest: command not found",
                                             is_error=True)] if failing else
                    [ToolResultBlock(tool_use_id="b1", content="3 failed, 10 passed")], parent_tool_use_id="t1"),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content=[{"type": "text", "text": "pytest missing"}])]),
        AssistantMessage(content=[TextBlock("All 13 tests pass.")], model="claude-sonnet-5-5", usage=usage),
        ResultMessage(subtype="success", duration_ms=900, duration_api_ms=700, is_error=False, num_turns=3,
                      session_id="s1", total_cost_usd=0.0123, usage={"input_tokens": 60, "output_tokens": 36},
                      result="All 13 tests pass."),
    ]


def test_claude_agent_sdk_stream_builds_tree_cost_and_subagent(mem):
    pytest.importorskip("claude_agent_sdk")
    from agent_tracedown.integrations import claude_agent as tdn_claude

    msgs = _claude_messages(failing=True)

    async def stream():
        for m in msgs:
            yield m

    async def consume():
        return [m async for m in tdn_claude.trace_stream(stream(), name="CodingAgent")]

    assert asyncio.run(consume()) == msgs  # passes through unchanged
    spans = ordered(mem)
    root = root_of(mem)
    assert root["attributes"]["tdn.run.cost_usd"] == pytest.approx(0.0123)
    assert root["attributes"]["gen_ai.request.model"] == "claude-sonnet-5-5"
    assert [t["name"] for t in root["attributes"]["tdn.tools.available"]] == ["Read", "Bash", "Task"]
    assert json.loads(mem.blobs[root["input_digest"]]) == "Why do the tests fail?"
    assert json.loads(mem.blobs[root["output_digest"]]) == "All 13 tests pass."

    sub = next(s for s in spans if s["kind"] == "agent" and s["parent_span_id"])
    assert sub["attributes"]["gen_ai.agent.name"] == "test-runner"
    bash = next(s for s in spans if s["kind"] == "tool")
    assert bash["parent_span_id"] == sub["span_id"]  # the subagent's tool call nests under it
    assert bash["status"]["code"] == "ERROR" and "command not found" in bash["status"]["message"]
    llms = [s for s in spans if s["kind"] == "llm"]
    assert len(llms) == 3
    assert llms[0]["attributes"]["gen_ai.usage.input_tokens"] == 120  # cache reads count as input
    assert llms[1]["parent_span_id"] == sub["span_id"]

    report = rca(mem)
    rc = report["root_cause"]
    assert rc["span_id"] == bash["span_id"], rc


def test_claude_agent_sdk_error_result_fails_the_run(mem):
    pytest.importorskip("claude_agent_sdk")
    from claude_agent_sdk import ResultMessage, UserMessage

    from agent_tracedown.integrations import claude_agent as tdn_claude

    tdn_claude.trace_messages([UserMessage(content="hi"),
                               ResultMessage(subtype="error_max_turns", duration_ms=1, duration_api_ms=1,
                                             is_error=True, num_turns=10, session_id="s",
                                             errors=["reached max turns"])], name="A")
    root = root_of(mem)
    assert root["status"]["code"] == "ERROR" and "max turns" in root["status"]["message"]
    assert root["attributes"]["tdn.run.outcome"] == "failure"


# ----------------------------------------------------------------------------- Google ADK

def _adk_run(mem, tool_result):
    pytest.importorskip("google.adk")
    from google.adk.agents import LlmAgent
    from google.adk.apps import App
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    from agent_tracedown.integrations import google_adk as tdn_adk

    class FakeGemini(BaseLlm):
        model: str = "fake-gemini"

        async def generate_content_async(self, llm_request, stream=False):
            last = llm_request.contents[-1]
            usage = types.GenerateContentResponseUsageMetadata(prompt_token_count=40, candidates_token_count=6)
            if any(getattr(p, "function_response", None) for p in last.parts):
                parts = [types.Part(text="Order 42 was refunded $80.")]
            else:
                parts = [types.Part(function_call=types.FunctionCall(id="c1", name="lookup_order",
                                                                     args={"order_id": 42}))]
            yield LlmResponse(content=types.Content(role="model", parts=parts), usage_metadata=usage)

    def lookup_order(order_id: int) -> dict:
        """Look up an order by id."""
        return tool_result

    refunds = LlmAgent(name="Refunds", model=FakeGemini(), tools=[lookup_order], instruction="refunds",
                       description="handles refunds")
    root_agent = refunds

    async def main():
        app = App(name="app", root_agent=root_agent, plugins=[tdn_adk.TracedownPlugin()])
        runner = InMemoryRunner(app=app)
        s = await runner.session_service.create_session(app_name="app", user_id="u")
        msg = types.Content(role="user", parts=[types.Part(text="refund order 42")])
        async for _ in runner.run_async(user_id="u", session_id=s.id, new_message=msg):
            pass

    asyncio.run(main())
    return ordered(mem)


def test_google_adk_plugin_records_agents_models_tools(mem):
    spans = _adk_run(mem, {"status": "ok", "paid": 80})
    kinds = [s["kind"] for s in spans]
    assert kinds == ["agent", "agent", "llm", "tool", "llm"]
    agent = spans[1]
    assert agent["attributes"]["gen_ai.agent.name"] == "Refunds"
    tool = spans[3]
    assert tool["parent_span_id"] == agent["span_id"] and tool["status"]["code"] == "OK"
    assert json.loads(mem.blobs[tool["input_digest"]]) == {"order_id": 42}
    assert spans[2]["attributes"]["gen_ai.usage.input_tokens"] == 40
    root = root_of(mem)
    assert json.loads(mem.blobs[root["output_digest"]]) == "Order 42 was refunded $80."
    assert root["attributes"]["tdn.tools.available"][0]["name"] == "lookup_order"
    assert root["attributes"]["tdn.run.outcome"] == "success"


def test_google_adk_tool_error_status_is_the_root_cause(mem):
    spans = _adk_run(mem, {"status": "error", "error_message": "order 42 not found"})
    tool = next(s for s in spans if s["kind"] == "tool")
    assert tool["status"] == {"code": "ERROR", "message": "order 42 not found"}
    # the agent then claims a refund it never made: mark the run failed as a user would
    root = root_of(mem)
    root["status"] = {"code": "ERROR", "message": "customer: no refund arrived"}
    root["attributes"]["tdn.run.outcome"] = "failure"
    assert rca(mem)["root_cause"]["span_id"] == tool["span_id"]


# ----------------------------------------------------------------------------- Pydantic AI

def _pai_agent(tool_fails: bool):
    pytest.importorskip("pydantic_ai")
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    def model(messages, info: AgentInfo):
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("lookup_order", {"order_id": 42})])
        return ModelResponse(parts=[TextPart("Order 42 was refunded $80.")])

    agent = Agent(FunctionModel(model), name="Support")

    @agent.tool_plain
    def lookup_order(order_id: int) -> dict:
        """Look up an order by id."""
        if tool_fails:
            raise RuntimeError("orders database timed out")
        return {"order_id": order_id, "paid": 80}

    return agent


def test_pydantic_ai_success(mem, monkeypatch):
    monkeypatch.setenv("PYDANTIC_AI_NO_BANNER", "1")
    from agent_tracedown.integrations import pydantic_ai as tdn_pai

    result = tdn_pai.run_sync(_pai_agent(False), "refund order 42")
    assert result.output == "Order 42 was refunded $80."
    spans = ordered(mem)
    assert [s["kind"] for s in spans] == ["agent", "llm", "tool", "llm"]
    tool = spans[2]
    assert json.loads(mem.blobs[tool["input_digest"]]) == {"order_id": 42}
    assert json.loads(mem.blobs[tool["output_digest"]]) == {"order_id": 42, "paid": 80}
    assert all(s["attributes"].get("gen_ai.usage.input_tokens") for s in spans if s["kind"] == "llm")
    root = root_of(mem)
    assert root["attributes"]["gen_ai.agent.name"] == "Support"
    assert json.loads(mem.blobs[root["output_digest"]]) == "Order 42 was refunded $80."
    assert root["attributes"]["tdn.tools.available"][0]["name"] == "lookup_order"


def test_pydantic_ai_crash_is_recorded_on_the_tool(mem, monkeypatch):
    monkeypatch.setenv("PYDANTIC_AI_NO_BANNER", "1")
    from agent_tracedown.integrations import pydantic_ai as tdn_pai

    with pytest.raises(RuntimeError, match="timed out"):
        tdn_pai.run_sync(_pai_agent(True), "refund order 42")
    tool = next(s for s in mem.spans if s["kind"] == "tool")
    assert tool["status"]["code"] == "ERROR" and "timed out" in tool["status"]["message"]
    assert root_of(mem)["attributes"]["tdn.run.outcome"] == "failure"
    assert rca(mem)["root_cause"]["span_id"] == tool["span_id"]


def test_pydantic_ai_retried_tool_marked_failed(mem):
    pytest.importorskip("pydantic_ai")
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart

    from agent_tracedown.integrations import pydantic_ai as tdn_pai

    history = [
        ModelRequest(parts=[UserPromptPart("book Friday")]),
        ModelResponse(parts=[ToolCallPart("book", {"date": "Friday"}, tool_call_id="x")], model_name="m"),
        ModelRequest(parts=[ToolReturnPart("book", "date must be ISO-8601", tool_call_id="x", outcome="retried")]),
        ModelResponse(parts=[TextPart("Booked.")], model_name="m"),
    ]
    tdn_pai.trace_messages(history, name="Booker")
    tool = next(s for s in mem.spans if s["kind"] == "tool")
    assert tool["status"]["code"] == "ERROR" and tool["attributes"]["tdn.tool.outcome"] == "retried"


# ----------------------------------------------------------------------------- MCP

def _mcp_server():
    pytest.importorskip("mcp")
    from mcp.server.mcpserver import MCPServer

    srv = MCPServer("billing")

    @srv.tool()
    def refund(order_id: int, amount: float) -> dict:
        """Refund an order."""
        if amount > 50:
            raise ValueError("refund limit is $50")
        return {"ok": True, "refunded": amount}

    @srv.tool()
    def lookup_order(order_id: int) -> dict:
        """Look up an order (read-only)."""
        return {"order_id": order_id, "paid": 80}

    return srv


def test_mcp_client_tool_calls_and_tools_declared(mem):
    srv = _mcp_server()
    from mcp import Client

    from agent_tracedown.integrations import mcp as tdn_mcp

    async def main():
        async with Client(srv) as client:
            tdn_mcp.instrument(client, server="billing")
            tdn_mcp.instrument(client, server="billing")  # idempotent
            with tdn.run("Support", input="refund order 42") as run:
                await client.list_tools()
                await client.call_tool("lookup_order", {"order_id": 42})
                bad = await client.call_tool("refund", {"order_id": 42, "amount": 80})
                assert bad.is_error
                run.output("Refunded $80.")
                run.fail("customer: refund never arrived")
            await client.call_tool("refund", {"order_id": 2, "amount": 1})  # outside a run: not recorded

    asyncio.run(main())
    tools = [s for s in ordered(mem) if s["kind"] == "tool"]
    assert len(tools) == 2
    assert tools[0]["status"]["code"] == "OK" and tools[0]["attributes"]["tdn.mcp.server"] == "billing"
    assert json.loads(mem.blobs[tools[0]["output_digest"]]) == {"order_id": 42, "paid": 80}
    assert tools[1]["status"]["code"] == "ERROR"
    declared = root_of(mem)["attributes"]["tdn.tools.available"]
    refund = next(t for t in declared if t["name"] == "refund")
    assert refund["schema"]["required"] == ["order_id", "amount"] and refund["description"] == "Refund an order."
    assert rca(mem)["root_cause"]["span_id"] == tools[1]["span_id"]


def test_mcp_instrument_all_patches_client_class(mem):
    srv = _mcp_server()
    import mcp

    from agent_tracedown.integrations import mcp as tdn_mcp

    orig = {c: (c.__dict__["call_tool"], c.__dict__["list_tools"]) for c in (mcp.Client, mcp.ClientSession)}
    try:
        tdn_mcp.instrument_all()
        tdn_mcp.instrument_all()  # idempotent

        async def main():
            async with mcp.Client(srv) as client:
                with tdn.run("Support"):
                    await client.call_tool("refund", {"order_id": 1, "amount": 5})

        asyncio.run(main())
        assert [s["name"] for s in mem.spans if s["kind"] == "tool"] == ["execute_tool refund"]
    finally:
        for c, (call, lst) in orig.items():
            c.call_tool, c.list_tools = call, lst
