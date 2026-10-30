"""Drives the real Agents SDK tracing machinery (spans created by the SDK's own helpers)."""
import json

import pytest

pytest.importorskip("agents")
from agents.tracing import (  # noqa: E402
    agent_span, function_span, generation_span, guardrail_span, handoff_span, trace,
)

import agent_tracedown as tdn  # noqa: E402
from agent_tracedown.integrations import openai_agents as tdn_oa  # noqa: E402


def test_agents_sdk_trace_maps_to_abx_run():
    mem = tdn.MemoryExporter()
    tdn.init(project="t", exporter=mem)
    proc = tdn_oa.instrument(replace=True)
    with trace("Refund workflow"):
        with agent_span(name="Triage", handoffs=["Refunds"], tools=[]):
            with guardrail_span(name="pii_check", triggered=False):
                pass
            with handoff_span(from_agent="Triage", to_agent="Refunds"):
                pass
        with agent_span(name="Refunds", tools=["lookup_order"]):
            with generation_span(model="gpt-4.1-mini", input=[{"role": "user", "content": "refund order 42"}]) as g:
                g.span_data.output = [{"role": "assistant", "content": "calling lookup_order"}]
                g.span_data.usage = {"input_tokens": 50, "output_tokens": 7}
            with function_span(name="lookup_order", input='{"order_id": 42}') as f:
                f.span_data.output = '{"status": "shipped"}'
                f.set_error({"message": "timeout", "data": {"after_s": 10}})
    assert len(proc.trace_ids) == 1
    spans = mem.trace(proc.trace_ids[0])
    by = {s["name"]: s for s in spans}
    assert "invoke_agent Triage" in by and "invoke_agent Refunds" in by
    assert by["handoff Triage -> Refunds"]["attributes"]["tdn.handoff.to"] == "Refunds"
    assert by["decision guardrail pii_check"]["attributes"]["tdn.decision.chosen"] == "pass"
    gen = by["chat gpt-4.1-mini"]
    assert gen["attributes"]["gen_ai.usage.input_tokens"] == 50 and gen["attributes"]["tdn.agent.name"] == "Refunds"
    tool = by["execute_tool lookup_order"]
    assert tool["status"]["code"] == "ERROR" and "timeout" in tool["status"]["message"]
    assert json.loads(mem.blobs[tool["input_digest"]]) == {"order_id": 42}
    root = next(s for s in spans if s["parent_span_id"] is None)
    assert root["attributes"]["gen_ai.agent.name"] == "Refund workflow"
    assert [t["name"] for t in root["attributes"]["tdn.tools.available"]] == ["lookup_order"]


def test_chat_completions_output_keeps_text_and_tool_calls():
    """LiteLLM models report chat-completions messages with provider extras."""
    mem = tdn.MemoryExporter()
    tdn.init(project="t", exporter=mem)
    proc = tdn_oa.instrument(replace=True)
    with trace("Support"):
        with agent_span(name="Triage", tools=[]):
            with generation_span(model="anthropic/claude", input=[{"role": "user", "content": "where is my bag?"}]) as g:
                g.span_data.output = [{"role": "assistant", "content": None,
                                       "provider_specific_fields": {"thinking_blocks": [{"signature": "x" * 500}]},
                                       "tool_calls": [{"id": "c1", "type": "function",
                                                       "function": {"name": "faq_lookup", "arguments": "{\"q\": \"bag\"}"}}]}]
    spans = mem.trace(proc.trace_ids[0])
    gen = next(s for s in spans if s["kind"] == "llm")
    out = json.loads(mem.blobs[gen["output_digest"]])
    assert out == {"text": '[calls faq_lookup({"q": "bag"})]'}
    root = next(s for s in spans if s["parent_span_id"] is None)
    assert json.loads(mem.blobs[root["input_digest"]]) == "where is my bag?"
