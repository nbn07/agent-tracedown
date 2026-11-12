"""smolagents: a real ToolCallingAgent driven by a fake model (no network)."""
import json

import pytest

smolagents = pytest.importorskip("smolagents")
from smolagents import Model, ChatMessage, ToolCallingAgent, tool  # noqa: E402
from smolagents.models import TokenUsage  # noqa: E402

import agent_tracedown as tdn  # noqa: E402
from agent_tracedown.integrations import smolagents as tdn_smol  # noqa: E402


@pytest.fixture()
def mem():
    m = tdn.MemoryExporter()
    tdn.init(project="t", exporter=m)
    return m


def _call(name, args):
    return ChatMessage(role="assistant", content="",
                       tool_calls=[{"id": f"c-{name}", "type": "function",
                                    "function": {"name": name, "arguments": args}}],
                       token_usage=TokenUsage(input_tokens=10, output_tokens=5))


class FakeModel(Model):
    """Returns the scripted ChatMessages, then a final answer."""

    def __init__(self, script):
        super().__init__()
        self.script = list(script)
        self.model_id = "fake-model"

    def generate(self, messages, stop_sequences=None, response_format=None, tools_to_call_from=None, **kw):
        if self.script:
            return self.script.pop(0)
        return _call("final_answer", {"answer": "no answer"})


@tool
def get_weather(city: str) -> str:
    """Get the weather in a city.

    Args:
        city: The city to look up.
    """
    return "sunny, 24C"


@tool
def get_broken_weather(city: str) -> str:
    """Get the weather in a city from a service that is down.

    Args:
        city: The city to look up.
    """
    raise RuntimeError("weather service down")


def ordered(mem, trace_id):
    return sorted(mem.trace(trace_id), key=lambda s: s["attributes"]["tdn.step"])


def test_smolagents_run_becomes_one_run(mem):
    model = FakeModel([_call("get_weather", {"city": "Paris"}),
                       _call("final_answer", {"answer": "It is sunny in Paris."})])
    agent = ToolCallingAgent(tools=[get_weather], model=model, max_steps=4)
    recorder = tdn_smol.instrument(agent)
    assert agent.run("Weather in Paris?") == "It is sunny in Paris."

    spans = ordered(mem, recorder.trace_ids[0])
    assert [s["kind"] for s in spans] == ["agent", "llm", "tool", "llm", "tool"]
    llm = next(s for s in spans if s["kind"] == "llm")
    assert llm["attributes"]["gen_ai.request.model"] == "fake-model"
    assert llm["attributes"]["gen_ai.usage.input_tokens"] == 10
    assert llm["attributes"]["gen_ai.usage.output_tokens"] == 5
    tool_span = next(s for s in spans if s["attributes"].get("gen_ai.tool.name") == "get_weather")
    assert json.loads(mem.blobs[tool_span["input_digest"]]) == {"city": "Paris"}
    assert json.loads(mem.blobs[tool_span["output_digest"]]) == "sunny, 24C"
    root = next(s for s in spans if s["parent_span_id"] is None)
    assert root["attributes"]["tdn.run.outcome"] == "success"
    declared = {t["name"]: t for t in root["attributes"]["tdn.tools.available"]}
    assert declared["get_weather"]["schema"]["properties"]["city"] == {"type": "string"}


def test_smolagents_failing_tool_is_recorded_and_diagnosed(mem):
    model = FakeModel([_call("get_broken_weather", {"city": "Paris"}),
                       _call("get_broken_weather", {"city": "Paris"})])
    agent = ToolCallingAgent(tools=[get_broken_weather], model=model, max_steps=2)
    recorder = tdn_smol.instrument(agent)
    agent.run("Weather in Paris?")  # the agent gives up at max steps and returns ''

    spans = ordered(mem, recorder.trace_ids[0])
    tools = [s for s in spans if s["kind"] == "tool"]
    assert len(tools) == 2
    for t in tools:
        assert t["attributes"]["gen_ai.tool.name"] == "get_broken_weather"
        assert t["status"]["code"] == "ERROR" and "weather service down" in t["status"]["message"]
        assert json.loads(mem.blobs[t["input_digest"]]) == {"city": "Paris"}
    root = next(s for s in spans if s["parent_span_id"] is None)
    assert root["attributes"]["tdn.run.outcome"] == "failure"
    engine = pytest.importorskip("tracedown_server.rca.engine")
    rep = engine.analyze(mem.trace(recorder.trace_ids[0]), mem.blobs)
    assert rep["root_cause"]["category"] == "tool_failure"
    assert rep["root_cause"]["span_id"] == tools[0]["span_id"]


def test_smolagents_instrumenting_twice_is_a_no_op(mem):
    agent = ToolCallingAgent(tools=[get_weather], model=FakeModel([]), max_steps=1)
    first = tdn_smol.instrument(agent)
    assert tdn_smol.instrument(agent) is first
