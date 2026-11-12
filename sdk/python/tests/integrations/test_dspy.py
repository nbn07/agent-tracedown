"""DSPy: a real program with a DummyLM (no network), a failing tool, and ReAct's tools."""
import json

import pytest

dspy = pytest.importorskip("dspy")
from dspy.utils.dummies import DummyLM  # noqa: E402

import agent_tracedown as tdn  # noqa: E402
from agent_tracedown.integrations import dspy as tdn_dspy  # noqa: E402


@pytest.fixture()
def mem():
    m = tdn.MemoryExporter()
    tdn.init(project="t", exporter=m)
    return m


def ordered(mem, trace_id):
    return sorted(mem.trace(trace_id), key=lambda s: s["attributes"]["tdn.step"])


class QA(dspy.Module):
    def __init__(self):
        super().__init__()
        self.gen = dspy.Predict("question -> answer")

    def forward(self, question):
        return self.gen(question=question)


def test_dspy_program_becomes_one_run(mem):
    handler = tdn_dspy.callback("QAProgram")
    with dspy.context(lm=DummyLM([{"answer": "Paris"}]), callbacks=[handler]):
        out = QA()(question="Capital of France?")
    assert out.answer == "Paris"
    spans = ordered(mem, handler.trace_ids[0])
    assert [s["kind"] for s in spans] == ["agent", "agent", "llm"]
    root, _, llm = spans
    assert root["name"] == "invoke_agent QAProgram" and root["parent_span_id"] is None
    assert llm["name"] == "chat dummy" and llm["attributes"]["gen_ai.request.model"] == "dummy"
    assert "Paris" in json.loads(mem.blobs[llm["output_digest"]])["text"]
    assert root["attributes"]["tdn.run.outcome"] == "success"


def explode(city: str) -> str:
    """Look up a city."""
    raise RuntimeError("lookup service down")


class CityQA(dspy.Module):
    def __init__(self):
        super().__init__()
        self.lookup = dspy.Tool(explode)
        self.gen = dspy.Predict("question -> answer")

    def forward(self, question):
        info = self.lookup(city="Paris")
        return self.gen(question=f"{question} (context: {info})")


def test_dspy_failing_tool_is_recorded_and_diagnosed(mem):
    handler = tdn_dspy.callback()
    with dspy.context(lm=DummyLM([{"answer": "Paris"}]), callbacks=[handler]):
        with pytest.raises(RuntimeError, match="lookup service down"):
            CityQA()(question="Capital of France?")
    spans = ordered(mem, handler.trace_ids[0])
    tool = next(s for s in spans if s["kind"] == "tool")
    assert tool["attributes"]["gen_ai.tool.name"] == "explode"
    assert tool["status"]["code"] == "ERROR" and "lookup service down" in tool["status"]["message"]
    assert json.loads(mem.blobs[tool["input_digest"]]) == {"city": "Paris"}
    root = next(s for s in spans if s["parent_span_id"] is None)
    assert root["attributes"]["tdn.run.outcome"] == "failure"
    engine = pytest.importorskip("tracedown_server.rca.engine")
    rep = engine.analyze(mem.trace(handler.trace_ids[0]), mem.blobs)
    assert rep["root_cause"]["category"] == "tool_failure"
    assert rep["root_cause"]["span_id"] == tool["span_id"]


def test_dspy_react_tools_are_declared_on_the_run(mem):
    def get_population(city: str) -> str:
        """Get the population of a city."""
        return "2.1 million"

    # A real ReAct instance carries the tools offered to the model; drive the
    # handler's module events with it directly (DummyLM cannot produce ReAct's
    # structured tool_calls output in this DSPy version).
    react = dspy.ReAct("question -> answer", tools=[get_population])
    handler = tdn_dspy.callback()
    handler.on_module_start("call-1", react, {"question": "Population of Paris?"})
    handler.on_module_end("call-1", outputs=None)
    root = next(s for s in mem.trace(handler.trace_ids[0]) if s["parent_span_id"] is None)
    declared = {t["name"]: t for t in root["attributes"]["tdn.tools.available"]}
    assert declared["get_population"]["description"] == "Get the population of a city."
    assert declared["get_population"]["schema"]["properties"]["city"] == {"type": "string"}


def test_dspy_instrument_registers_the_handler_once(mem):
    before = list(dspy.settings.callbacks or [])
    try:
        handler = tdn_dspy.instrument()
        assert handler in dspy.settings.callbacks
        tdn_dspy.instrument()
        handlers = [c for c in dspy.settings.callbacks if isinstance(c, tdn_dspy.TracedownCallbackHandler)]
        assert len(handlers) == 1
    finally:
        dspy.configure(callbacks=before)
