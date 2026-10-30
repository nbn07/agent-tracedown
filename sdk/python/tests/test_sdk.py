import json

import pytest

import agent_tracedown as tdn
from agent_tracedown.redaction import default_redactor


@pytest.fixture()
def mem():
    exporter = tdn.MemoryExporter()
    tdn.init(project="test", exporter=exporter)
    return exporter


def _root(spans):
    return next(s for s in spans if s["parent_span_id"] is None)


def test_run_captures_steps_tree_and_otel_attributes(mem):
    with tdn.run("Bot", input="hi") as run:
        with tdn.agent("Planner"):
            tdn.llm_call(lambda p, **_: {"text": "ok", "usage": {"input_tokens": 5, "output_tokens": 1}},
                         prompt="hi", model="m1", context_limit=10)
            tdn.tool_call("echo", {"x": 1}, lambda x: x)
        run.output("done")
    spans = mem.trace(run.trace_id)
    assert [s["attributes"]["tdn.step"] for s in spans] == [1, 2, 3, 0]  # root span is exported last
    root = _root(spans)
    assert root["attributes"]["tdn.run.outcome"] == "success"
    assert root["attributes"]["gen_ai.operation.name"] == "invoke_agent"
    llm = next(s for s in spans if s["kind"] == "llm")
    assert llm["attributes"]["gen_ai.operation.name"] == "chat"
    assert llm["attributes"]["gen_ai.usage.input_tokens"] == 5
    assert llm["attributes"]["tdn.llm.context_utilization"] == 0.5
    assert llm["attributes"]["tdn.agent.name"] == "Planner"
    planner = next(s for s in spans if s["name"] == "invoke_agent Planner")
    assert llm["parent_span_id"] == planner["span_id"]
    assert json.loads(mem.blobs[root["output_digest"]]) == "done"


def test_tool_schema_validation_marks_error_and_raises(mem):
    schema = {"required": ["start"], "properties": {"start": {"type": "string", "format": "date-time"}}}
    with pytest.raises(tdn.ToolError):
        with tdn.run("Bot"):
            tdn.tool_call("cal", {"start": "Friday"}, lambda start: None, schema=schema)
    spans = mem.spans
    tool = next(s for s in spans if s["kind"] == "tool")
    assert tool["status"]["code"] == "ERROR"
    assert tool["attributes"]["tdn.tool.args_valid"] is False
    assert "ISO-8601" in tool["status"]["message"]
    assert _root(spans)["attributes"]["tdn.run.outcome"] == "failure"


def test_calls_outside_a_run_are_untraced_but_work(mem):
    assert tdn.tool_call("t", {"a": 2}, lambda a: a * 2) == 4
    assert mem.spans == []


def test_identical_payloads_are_stored_once(mem):
    with tdn.run("Bot"):
        for _ in range(3):
            tdn.llm_call(lambda p, **_: "same", prompt="same prompt", model="m")
    assert len(mem.blobs) == 2  # prompt + output, deduplicated by digest


def test_redaction_and_capture_off():
    exporter = tdn.MemoryExporter()
    tdn.init(exporter=exporter, redact=default_redactor)
    with tdn.run("Bot", input="mail me at ana@example.com, key sk-abcdefghijklmnopqrstu"):
        pass
    text = " ".join(exporter.blobs.values())
    assert "ana@example.com" not in text and "[EMAIL]" in text and "[API_KEY]" in text

    exporter2 = tdn.MemoryExporter()
    tdn.init(exporter=exporter2, capture_content=False)
    with tdn.run("Bot", input="secret"):
        pass
    assert exporter2.blobs == {} and exporter2.spans[0]["input_digest"].startswith("sha256:")


def _agent(llm_fn):
    with tdn.run("Bot", input="q") as run:
        a = tdn.llm_call(llm_fn, prompt="q", model="m")
        b = tdn.tool_call("lookup", {"key": a}, lambda key: {"value": key.upper()})
        run.output(b["value"])
        return run.trace_id


def test_exact_replay_serves_every_step_from_the_recording(mem):
    tid = _agent(lambda p, **_: "alpha")
    bundle = {"spans": mem.trace(tid), "blobs": dict(mem.blobs)}
    calls = []
    with tdn.replay(bundle, mode="exact"):
        new = _agent(lambda p, **_: calls.append(1) or "SHOULD NOT RUN")
    root = _root(mem.trace(new))
    assert calls == []
    assert root["attributes"]["tdn.replay.of"] == tid
    assert root["attributes"]["tdn.replay.recorded_steps"] == [1, 2]
    assert json.loads(mem.blobs[root["output_digest"]]) == "ALPHA"


def test_swap_replay_runs_llm_live_and_tools_diverge_when_inputs_change(mem):
    tid = _agent(lambda p, **_: "alpha")
    bundle = {"spans": mem.trace(tid), "blobs": dict(mem.blobs)}
    with tdn.replay(bundle, mode="swap", overrides={"model": "m2", "llm_fn": lambda p, **_: "beta"}):
        new = _agent(lambda p, **_: "alpha")
    spans = mem.trace(new)
    root = _root(spans)
    assert root["attributes"]["tdn.replay.live_steps"] == [1, 2]
    assert root["attributes"]["tdn.replay.first_divergence"] == 2
    assert next(s for s in spans if s["kind"] == "llm")["attributes"]["gen_ai.request.model"] == "m2"
    assert json.loads(mem.blobs[root["output_digest"]]) == "BETA"


def test_exact_replay_detects_changed_agent_code(mem):
    tid = _agent(lambda p, **_: "alpha")
    bundle = {"spans": mem.trace(tid), "blobs": dict(mem.blobs)}

    def changed():
        with tdn.run("Bot", input="q"):
            tdn.llm_call(lambda p, **_: "x", prompt="a different prompt", model="m")

    with tdn.replay(bundle, mode="exact"), pytest.raises(tdn.ReplayDivergence):
        changed()


def test_otlp_export_shape(mem):
    with tdn.run("Bot") as run:
        tdn.tool_call("t", {}, lambda: 1)
    body = tdn.to_otlp_json(mem.trace(run.trace_id))
    spans = body["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert {s["name"] for s in spans} == {"invoke_agent Bot", "execute_tool t"}
    tool = next(s for s in spans if s["name"] == "execute_tool t")
    keys = {a["key"] for a in tool["attributes"]}
    assert {"gen_ai.operation.name", "gen_ai.tool.name", "tdn.kind"} <= keys


def test_openai_plugin_wraps_client(mem):
    class Completions:
        def create(self, **kw):
            return {"text": f"echo {kw['messages'][0]['content']}", "usage": {"input_tokens": 3, "output_tokens": 2}}

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    client = tdn.plugins.instrument("openai", Client())
    with tdn.run("Bot"):
        out = client.chat.completions.create(model="gpt-x", messages=[{"role": "user", "content": "hi"}])
    assert out["text"] == "echo hi"
    llm = next(s for s in mem.spans if s["kind"] == "llm")
    assert llm["attributes"]["gen_ai.provider.name"] == "openai"
    assert llm["attributes"]["gen_ai.request.model"] == "gpt-x"
