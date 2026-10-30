import os
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [os.path.join(ROOT, "sdk", "python"), os.path.join(ROOT, "examples")]

import agent_tracedown as tdn  # noqa: E402
import demo_agents as demo  # noqa: E402
from tracedown_server.api import create_app  # noqa: E402
from tracedown_server.rca import analyze  # noqa: E402
from tracedown_server.storage import SQLiteStore  # noqa: E402


@pytest.fixture()
def mem():
    exporter = tdn.MemoryExporter()
    tdn.init(project="demo", exporter=exporter)
    return exporter


def _run(mem, fn, **kw):
    try:
        fn(**kw)
    except tdn.ToolError:
        pass
    tid = mem.spans[-1]["trace_id"]  # spans of the latest run
    return tid, mem.trace(tid)


# ----------------------------------------------------------------------------- RCA engine

def test_rca_blames_the_llm_step_that_produced_the_bad_argument(mem):
    tid, spans = _run(mem, demo.calendar_agent)
    r = analyze(spans, mem.blobs)
    top = r["root_cause"]
    assert top["category"] == "bad_llm_output" and top["step"] == 4 and top["confidence"] >= 0.85
    symptom = next(f for f in r["findings"] if f["symptom"])
    assert symptom["step"] == 5 and symptom["caused_by"] == top["id"]
    assert "AgentFailureException: Invalid tool arguments" in r["stack_trace"]
    assert "caused by step 4" in r["stack_trace"]


def test_rca_walks_back_from_reported_failure_to_stale_source(mem):
    _, spans = _run(mem, demo.refund_agent)
    top = analyze(spans, mem.blobs)["root_cause"]
    assert top["category"] == "stale_source" and "refund-policy-2023.md" in top["title"]


def test_rca_detects_loop(mem):
    _, spans = _run(mem, demo.research_loop_agent)
    top = analyze(spans, mem.blobs)["root_cause"]
    assert top["category"] == "loop" and "4 times" in top["title"]


def test_rca_flags_number_missing_from_evidence(mem):
    _, spans = _run(mem, demo.pricing_agent)
    top = analyze(spans, mem.blobs)["root_cause"]
    assert top["category"] == "hallucination" and "49" in " ".join(top["evidence"])


def test_successful_grounded_run_has_no_findings(mem):
    with tdn.run("Bot", input="price of pro?") as run:
        p = tdn.tool_call("get_price", {"plan": "pro"}, lambda plan: {"usd": 39})
        run.output(f"The pro plan is ${p['usd']} per month.")
    r = analyze(mem.trace(run.trace_id), mem.blobs)
    assert r["findings"] == [] and r["stack_trace"].startswith("No failure detected")


# ----------------------------------------------------------------------------- HTTP API

@pytest.fixture()
def client(mem):
    app = create_app(store=SQLiteStore(":memory:"), api_keys=[])
    return TestClient(app)


def _push(client, mem):
    client.post("/v1/ingest/blobs", json={"project": "demo", "blobs": mem.blobs})
    return client.post("/v1/ingest/spans", json={"project": "demo", "spans": mem.spans}).json()


def test_ingest_lists_and_auto_analyzes_failures(client, mem):
    tid, _ = _run(mem, demo.calendar_agent)
    res = _push(client, mem)
    assert res["accepted"] == 6  # writer steps never ran and res["analyzed"] == [tid]
    runs = client.get("/api/v1/runs?outcome=failure").json()["runs"]
    assert runs[0]["trace_id"] == tid and runs[0]["root_cause"]["category"] == "bad_llm_output"
    assert runs[0]["agent_name"] == "CalendarAssistant" and runs[0]["models"] == ["gpt-4o-mini"]
    trace = client.get(f"/api/v1/runs/{tid}/stacktrace").text
    assert "root cause: Bad LLM output" in trace


def test_run_detail_graph_and_blob(client, mem):
    tid, _ = _run(mem, demo.calendar_agent)
    _push(client, mem)
    detail = client.get(f"/api/v1/runs/{tid}").json()
    assert detail["tree"][0]["name"] == "invoke_agent CalendarAssistant"
    graph = client.get(f"/api/v1/runs/{tid}/graph").json()
    ids = {n["id"] for n in graph["nodes"]}
    assert {"agent:CalendarAssistant", "agent:PlannerAgent", "tool:calendar_create_event"} <= ids
    tool_edge = next(e for e in graph["edges"] if e["target"] == "tool:calendar_create_event")
    assert tool_edge["source"] == "agent:PlannerAgent" and tool_edge["errors"] == 1
    d = next(s for s in detail["spans"] if s["kind"] == "tool")["input_digest"]
    assert client.get(f"/api/v1/blobs/{d}?trace_id={tid}").json()["content"]["start"] == "Friday"


def test_replay_plan_and_sdk_replay_roundtrip(client, mem):
    tid, _ = _run(mem, demo.calendar_agent)
    _push(client, mem)
    p = client.post(f"/api/v1/runs/{tid}/replay", json={"mode": "from-step", "from_step": 4}).json()
    assert [(s["step"], s["source"]) for s in p["steps"]] == [(2, "recorded"), (4, "live"), (5, "live")]
    assert p["replayable"] is True
    assert client.post(f"/api/v1/runs/{tid}/replay", json={"mode": "bogus"}).status_code == 422

    bundle = client.get(f"/api/v1/runs/{tid}/bundle").json()
    mem.clear()
    with tdn.replay(bundle, mode="from-step", from_step=4):
        new_tid = demo.calendar_agent(extractor=demo.extract_event_fixed)
    _push(client, mem)
    d = client.get(f"/api/v1/diff?a={tid}&b={new_tid}").json()
    assert d["outcome_a"] == "failure" and d["outcome_b"] == "success"
    assert d["first_divergence"] == 4


def test_otlp_ingest(client, mem):
    with tdn.run("OtelBot") as run:
        tdn.tool_call("t", {"a": 1}, lambda a: a)
    body = tdn.to_otlp_json(mem.trace(run.trace_id))
    assert client.post("/v1/traces", json=body).json()["accepted"] == 2
    spans = client.get(f"/api/v1/runs/{run.trace_id}").json()["spans"]
    assert {s["kind"] for s in spans} == {"agent", "tool"}


def test_api_key_auth():
    c = TestClient(create_app(store=SQLiteStore(":memory:"), api_keys=["tdn_test"]))
    assert c.get("/api/v1/runs").status_code == 401
    assert c.get("/api/v1/runs", headers={"Authorization": "Bearer tdn_test"}).status_code == 200
    assert c.get("/healthz").status_code == 200
