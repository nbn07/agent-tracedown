"""Runs a real LangGraph graph with a fake chat model (no network)."""
import pytest

pytest.importorskip("langgraph")
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: E402
from langgraph.prebuilt import ToolNode  # noqa: E402

import agent_tracedown as tdn  # noqa: E402
from agent_tracedown.integrations import langchain as tdn_lc  # noqa: E402


@tool
def get_price(plan: str) -> dict:
    """Look up the monthly price of a plan."""
    return {"plan": plan, "usd_per_month": 39}


def build():
    model = GenericFakeChatModel(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "get_price", "args": {"plan": "pro"}, "id": "call_1"}]),
        AIMessage(content="The Pro plan costs $49 per month."),
    ]))

    def planner(state):
        return {"messages": [model.invoke(state["messages"])]}

    def route(state):
        return "tools" if state["messages"][-1].tool_calls else END

    g = StateGraph(MessagesState)
    g.add_node("planner", planner)
    g.add_node("tools", ToolNode([get_price]))
    g.add_edge(START, "planner")
    g.add_conditional_edges("planner", route, ["tools", END])
    g.add_edge("tools", "planner")
    return g.compile()


def test_langgraph_run_becomes_one_trace_with_nodes_tools_and_handoffs():
    mem = tdn.MemoryExporter()
    tdn.init(project="t", exporter=mem)
    handler = tdn_lc.callback("PricingGraph")
    out = build().invoke({"messages": [HumanMessage("How much is Pro?")]}, config={"callbacks": [handler]})
    assert out["messages"][-1].content.startswith("The Pro plan")
    assert len(handler.trace_ids) == 1
    spans = mem.trace(handler.trace_ids[0])
    root = next(s for s in spans if s["parent_span_id"] is None)
    assert root["attributes"]["gen_ai.agent.name"] == "PricingGraph"
    names = [s["name"] for s in sorted(spans, key=lambda s: s["attributes"]["tdn.step"])]
    assert names.count("invoke_agent planner") == 2 and "invoke_agent tools" in names
    assert "execute_tool get_price" in names
    assert "handoff planner -> tools" in names and "handoff tools -> planner" in names
    tool = next(s for s in spans if s["kind"] == "tool")
    assert tool["attributes"]["tdn.agent.name"] == "tools" and tool["status"]["code"] == "OK"
    llm = [s for s in spans if s["kind"] == "llm"]
    assert len(llm) == 2 and all(s["attributes"]["tdn.agent.name"] == "planner" for s in llm)
    import json
    assert json.loads(mem.blobs[root["output_digest"]]) == "The Pro plan costs $49 per month."
    assert json.loads(mem.blobs[tool["output_digest"]])["usd_per_month"] == 39
