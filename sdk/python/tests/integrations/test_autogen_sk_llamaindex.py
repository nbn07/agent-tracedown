"""AutoGen (real message classes), Semantic Kernel (real kernel + native plugin) and
LlamaIndex (real index with mock LLM and embeddings). No network."""
import asyncio
import json

import pytest

import agent_tracedown as tdn


@pytest.fixture()
def mem():
    m = tdn.MemoryExporter()
    tdn.init(project="t", exporter=m)
    return m


def test_autogen_stream(mem):
    pytest.importorskip("autogen_agentchat")
    from autogen_agentchat.base import TaskResult
    from autogen_agentchat.messages import HandoffMessage, TextMessage, ToolCallExecutionEvent, ToolCallRequestEvent
    from autogen_core import FunctionCall
    from autogen_core.models import FunctionExecutionResult, RequestUsage

    from agent_tracedown.integrations import autogen as tdn_ag

    msgs = [
        TextMessage(source="user", content="Refund order 42"),
        ToolCallRequestEvent(source="triage", content=[FunctionCall(id="c1", name="lookup", arguments='{"order": 42}')],
                             models_usage=RequestUsage(prompt_tokens=30, completion_tokens=5)),
        ToolCallExecutionEvent(source="triage", content=[FunctionExecutionResult(call_id="c1", name="lookup",
                                                                                 content='{"paid": 80}', is_error=False)]),
        HandoffMessage(source="triage", target="refunds", content="over to refunds"),
        TextMessage(source="refunds", content="You will get $80 back.",
                    models_usage=RequestUsage(prompt_tokens=40, completion_tokens=8)),
    ]

    async def stream():
        for m in msgs:
            yield m
        yield TaskResult(messages=msgs, stop_reason="done")

    async def consume():
        return [m async for m in tdn_ag.trace_stream(stream(), name="SupportTeam")]

    out = asyncio.run(consume())
    assert len(out) == 6
    spans = mem.spans
    names = [s["name"] for s in sorted(spans, key=lambda s: s["attributes"]["tdn.step"])]
    assert "invoke_agent triage" in names and "invoke_agent refunds" in names
    assert "handoff triage -> refunds" in names
    tool = next(s for s in spans if s["kind"] == "tool")
    assert tool["status"]["code"] == "OK" and json.loads(mem.blobs[tool["output_digest"]]) == {"paid": 80}
    root = next(s for s in spans if s["parent_span_id"] is None)
    assert json.loads(mem.blobs[root["output_digest"]]) == "You will get $80 back."
    assert root["attributes"]["tdn.autogen.stop_reason"] == "done"
    assert sum(1 for s in spans if s["kind"] == "llm") == 2


def test_semantic_kernel_filter(mem):
    pytest.importorskip("semantic_kernel")
    from semantic_kernel import Kernel
    from semantic_kernel.functions import kernel_function

    from agent_tracedown.integrations import semantic_kernel as tdn_sk

    class Weather:
        @kernel_function(name="forecast", description="Forecast for a city")
        def forecast(self, city: str) -> str:
            return f"Sunny in {city}"

    kernel = Kernel()
    kernel.add_plugin(Weather(), plugin_name="weather")
    tdn_sk.instrument(kernel)

    async def go():
        with tdn.run("TravelAgent", input="weather in Lisbon?") as run:
            res = await kernel.invoke(plugin_name="weather", function_name="forecast", city="Lisbon")
            run.output(str(res))
        return run.trace_id

    tid = asyncio.run(go())
    spans = mem.trace(tid)
    tool = next(s for s in spans if s["kind"] == "tool")
    assert tool["name"] == "execute_tool weather.forecast"
    assert json.loads(mem.blobs[tool["input_digest"]]) == {"city": "Lisbon"}
    assert json.loads(mem.blobs[tool["output_digest"]]) == "Sunny in Lisbon"


def test_llamaindex_query(mem):
    pytest.importorskip("llama_index.core")
    from llama_index.core import Document, Settings, VectorStoreIndex
    from llama_index.core.embeddings import MockEmbedding
    from llama_index.core.llms import MockLLM

    from agent_tracedown.integrations import llamaindex as tdn_li

    Settings.llm = MockLLM(max_tokens=16)
    Settings.embed_model = MockEmbedding(embed_dim=8)
    st = tdn_li.instrument()
    index = VectorStoreIndex.from_documents([
        Document(text="Annual plans refund $80 within 30 days.", metadata={"file_name": "refunds-2026.md"}),
        Document(text="Monthly plans are not refundable.", metadata={"file_name": "billing.md"}),
    ])
    before = len(st.trace_ids)
    index.as_query_engine(similarity_top_k=2).query("How much is the annual refund?")
    new = st.trace_ids[before:]
    assert len(new) >= 1
    spans = [s for tid in new for s in mem.trace(tid)]
    retr = next(s for s in spans if s["kind"] == "retrieval")
    chunks = json.loads(mem.blobs[retr["output_digest"]])
    assert {c["source"] for c in chunks} == {"refunds-2026.md", "billing.md"}
    assert retr["attributes"]["tdn.retrieval.count"] == 2
    assert any(s["kind"] == "llm" for s in spans)
    root = next(s for s in spans if s["parent_span_id"] is None and s["trace_id"] == retr["trace_id"])
    assert "query" in root["name"].lower()
