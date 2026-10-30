"""Importers: OpenTelemetry (several attribute conventions) and Langfuse, into Tracedown runs."""
import base64
import json
import os

import pytest

import agent_tracedown as tdn
from tracedown_server.importers import import_observations, import_otlp
from tracedown_server.importers.cli import main as cli_main
from tracedown_server.importers.langfuse import load_file
from tracedown_server.importers.otel import load_otlp_file
from tracedown_server.rca import analyze

EX = os.path.join(os.path.dirname(__file__), "..", "..", "examples", "imports")


def _blob(blobs, digest):
    return json.loads(blobs[digest]) if digest in blobs else None


def _by_name(spans):
    return {s["name"]: s for s in spans}


def test_openinference_export_becomes_a_diagnosable_run():
    (tid, spans, blobs), = import_otlp(load_otlp_file(os.path.join(EX, "phoenix-otlp.json")), failed=True)
    kinds = sorted(s["kind"] for s in spans)
    assert kinds == ["agent", "llm", "llm", "llm", "tool", "tool"]
    root = next(s for s in spans if s["attributes"].get("tdn.run.root"))
    assert root["parent_span_id"] is None and root["attributes"]["gen_ai.agent.name"] == "SupportAgent"
    assert root["attributes"]["tdn.run.outcome"] == "failure"
    refund = _by_name(spans)["refund_order"]
    assert refund["attributes"]["gen_ai.tool.name"] == "refund_order"
    assert _blob(blobs, refund["input_digest"]) == {"order_id": "#1001"}
    first_llm = min((s for s in spans if s["kind"] == "llm"), key=lambda s: s["attributes"]["tdn.step"])
    assert _blob(blobs, first_llm["input_digest"])[1]["role"] == "user"
    assert "list_orders" in _blob(blobs, first_llm["output_digest"])["text"]
    assert first_llm["attributes"]["gen_ai.request.model"] == "gpt-4.1-mini"
    assert first_llm["attributes"]["gen_ai.usage.input_tokens"] == 412
    steps = sorted(s["attributes"]["tdn.step"] for s in spans)
    assert steps == list(range(len(spans)))
    rep = analyze(spans, blobs)
    assert rep["outcome"] == "failure" and rep["findings"]


def test_genai_semconv_with_base64_ids_and_events():
    tid = bytes(range(16))
    sid = lambda n: bytes([n] * 8)  # noqa: E731
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    kv = lambda k, v: {"key": k, "value": {"stringValue": v}}  # noqa: E731
    body = {"resourceSpans": [{"resource": {"attributes": [kv("service.name", "travel")]}, "scopeSpans": [{"spans": [
        {"traceId": b64(tid), "spanId": b64(sid(1)), "name": "invoke_agent Planner", "startTimeUnixNano": "1000",
         "endTimeUnixNano": "9000", "attributes": [kv("gen_ai.operation.name", "invoke_agent"), kv("gen_ai.agent.name", "Planner")]},
        {"traceId": b64(tid), "spanId": b64(sid(2)), "parentSpanId": b64(sid(1)), "name": "chat gpt-4o", "startTimeUnixNano": "2000",
         "endTimeUnixNano": "3000", "attributes": [kv("gen_ai.operation.name", "chat"), kv("gen_ai.request.model", "gpt-4o"),
                                                   {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "50"}}],
         "events": [{"name": "gen_ai.user.message", "attributes": [kv("content", "Book Paris for Friday")]},
                    {"name": "gen_ai.choice", "attributes": [kv("message", json.dumps({"content": "Booking Rome."}))]}]},
        {"traceId": b64(tid), "spanId": b64(sid(3)), "parentSpanId": b64(sid(1)), "name": "execute_tool book_trip",
         "startTimeUnixNano": "4000", "endTimeUnixNano": "5000",
         "attributes": [kv("gen_ai.operation.name", "execute_tool"), kv("gen_ai.tool.name", "book_trip"),
                        kv("gen_ai.tool.call.arguments", json.dumps({"city": "Rome"}))]},
    ]}]}]}
    (trace_id, spans, blobs), = import_otlp([body])
    assert trace_id == tid.hex() and all(s["trace_id"] == tid.hex() for s in spans)
    assert {s["span_id"] for s in spans} == {sid(n).hex() for n in (1, 2, 3)}
    chat = _by_name(spans)["chat gpt-4o"]
    assert _blob(blobs, chat["input_digest"]) == [{"role": "user", "content": "Book Paris for Friday"}]
    assert _blob(blobs, chat["output_digest"]) == {"text": "Booking Rome."}
    assert chat["attributes"]["gen_ai.usage.input_tokens"] == 50 and chat["attributes"]["tdn.agent.name"] == "Planner"
    tool = _by_name(spans)["execute_tool book_trip"]
    assert _blob(blobs, tool["input_digest"]) == {"city": "Rome"}
    # no error anywhere: imported as a success unless the caller says otherwise
    root = next(s for s in spans if s["parent_span_id"] is None)
    assert root["attributes"]["tdn.run.outcome"] == "success"


def test_openllmetry_flattened_prompts_and_generic_wrappers():
    kv = lambda k, v: {"key": k, "value": {"stringValue": v}}  # noqa: E731
    T = "ab" * 16
    body = {"resourceSpans": [{"scopeSpans": [{"spans": [
        {"traceId": T, "spanId": "01" * 8, "name": "trip.workflow", "startTimeUnixNano": "1", "endTimeUnixNano": "9",
         "attributes": [kv("traceloop.span.kind", "workflow")]},
        {"traceId": T, "spanId": "02" * 8, "parentSpanId": "01" * 8, "name": "step.task", "startTimeUnixNano": "2",
         "endTimeUnixNano": "8", "attributes": [kv("traceloop.span.kind", "task")]},
        {"traceId": T, "spanId": "03" * 8, "parentSpanId": "02" * 8, "name": "openai.chat", "startTimeUnixNano": "3",
         "endTimeUnixNano": "4", "attributes": [kv("gen_ai.request.model", "gpt-4o"), kv("gen_ai.prompt.0.role", "user"),
                                                kv("gen_ai.prompt.0.content", "hi"), kv("gen_ai.completion.0.content", "hello")]},
        {"traceId": T, "spanId": "04" * 8, "parentSpanId": "02" * 8, "name": "lookup", "startTimeUnixNano": "5",
         "endTimeUnixNano": "6", "attributes": [kv("traceloop.span.kind", "tool"), kv("traceloop.entity.name", "lookup"),
                                                kv("traceloop.entity.input", json.dumps({"q": "x"}))],
         "status": {"code": 2, "message": "boom"}},
    ]}]}]}
    (_, spans, blobs), = import_otlp([body])
    names = {s["name"]: s for s in spans}
    assert "step.task" not in names  # plumbing wrapper folded away
    assert names["openai.chat"]["kind"] == "llm" and names["openai.chat"]["parent_span_id"] == "01" * 8
    assert _blob(blobs, names["openai.chat"]["input_digest"]) == [{"role": "user", "content": "hi"}]
    assert names["lookup"]["kind"] == "tool" and names["lookup"]["status"]["code"] == "ERROR"
    root = names["trip.workflow"]
    assert root["kind"] == "agent" and root["attributes"]["tdn.run.outcome"] == "failure"


def test_tracedown_otlp_export_round_trips():
    mem = tdn.MemoryExporter()
    tdn.init(project="t", exporter=mem)
    with tdn.run("SupportBot", input="Refund my order #2 please") as run:
        tdn.tool_call("get_orders", {"user": "u1"}, lambda user: {"orders": ["#1", "#2"]})
        tdn.tool_call("refund_order", {"order_id": "#1"}, lambda order_id: {"ok": True})
        run.output("Refunded.")
        run.fail("customer: wrong order")
    native = mem.trace(run.trace_id)
    body = tdn.to_otlp_json(native)
    (_, spans, _), = import_otlp([body])
    assert sorted((s["kind"], s["attributes"]["tdn.step"], s["input_digest"]) for s in spans) == \
        sorted((s["kind"], s["attributes"]["tdn.step"], s["input_digest"]) for s in native)
    assert analyze(spans, mem.blobs)["root_cause"]["step"] == analyze(native, mem.blobs)["root_cause"]["step"]


def test_langfuse_trace_export():
    obs, trace = load_file(os.path.join(EX, "langfuse-trace.json"))
    (tid, spans, blobs), = import_observations(obs, trace)
    names = _by_name(spans)
    assert tid == "lf-trace-42" and "search-chain" not in names  # CHAIN wrapper folded
    search = names["web_search"]
    assert search["kind"] == "tool" and search["status"]["message"] == "search API timeout after 5s"
    assert search["parent_span_id"] == "obs-agent"
    answer = names["answer"]
    assert answer["kind"] == "llm" and _blob(blobs, answer["output_digest"])["text"].startswith("Acme Corp")
    assert answer["attributes"]["gen_ai.usage.output_tokens"] == 21
    root = next(s for s in spans if s["attributes"].get("tdn.run.root"))
    assert root["name"] == "ResearchAgent" and root["attributes"]["tdn.run.outcome"] == "failure"
    assert {"name": "web_search", "side_effects": False} in root["attributes"]["tdn.tools.available"]
    rep = analyze(spans, blobs)
    cats = [f["category"] for f in rep["findings"]]
    assert "hallucination" in cats and "tool_failure" in cats


def test_langfuse_observation_list_without_trace_object_gets_a_synthetic_root():
    obs = [{"id": "g1", "traceId": "t1", "type": "GENERATION", "name": "draft", "startTime": "2026-10-29T10:00:00Z",
            "endTime": "2026-10-29T10:00:01Z", "level": "DEFAULT", "input": "hi", "output": "hello"},
           {"id": "x1", "traceId": "t1", "type": "TOOL", "name": "save", "startTime": "2026-10-29T10:00:02Z",
            "level": "ERROR", "statusMessage": "disk full"}]
    (_, spans, _), = import_observations(obs)
    root = next(s for s in spans if s["parent_span_id"] is None)
    assert root["kind"] == "agent" and {s["parent_span_id"] for s in spans if s is not root} == {root["span_id"]}
    assert root["attributes"]["tdn.run.outcome"] == "failure"


def test_cli_prints_a_diagnosis(capsys, tmp_path):
    out = tmp_path / "runs.jsonl"
    assert cli_main(["langfuse-file", os.path.join(EX, "langfuse-trace.json"), "--save", str(out)]) == 0
    text = capsys.readouterr().out
    assert "ResearchAgent" in text and "suspects:" in text and "web_search" in text
    saved = [json.loads(line) for line in out.read_text().splitlines()]
    assert saved[0]["trace_id"] == "lf-trace-42" and saved[0]["spans"]


def test_otlp_endpoint_stores_content_for_imported_runs():
    from fastapi.testclient import TestClient
    from tracedown_server.api import create_app
    from tracedown_server.storage import SQLiteStore
    client = TestClient(create_app(store=SQLiteStore(":memory:")))
    body = load_otlp_file(os.path.join(EX, "phoenix-otlp.json"))[0]
    assert client.post("/v1/traces", json=body).json()["accepted"] == 6
    tid = "5b8efff798038103d269b633813fc60c"
    bundle = client.get(f"/api/v1/runs/{tid}/bundle").json()
    refund = next(s for s in bundle["spans"] if s["name"] == "refund_order")
    assert json.loads(bundle["blobs"][refund["input_digest"]]) == {"order_id": "#1001"}


@pytest.mark.parametrize("since", ["24h", "7d", "2026-10-01T00:00:00Z"])
def test_since_parsing(since):
    from tracedown_server.importers.cli import _since
    assert _since(since).tzinfo is not None


def test_langfuse_api_client_pages_and_falls_back(monkeypatch):
    from tracedown_server.importers import langfuse as lf
    obs, trace = load_file(os.path.join(EX, "langfuse-trace.json"))
    calls = []

    def fake_get(self, path, params=None):
        calls.append((path, dict(params or {})))
        if path == "/api/public/v2/observations" and (params or {}).get("level") == "ERROR":
            # two pages of error observations, the trace appearing twice
            if not params.get("cursor"):
                return {"data": [{"id": "e1", "traceId": "lf-trace-42"}], "meta": {"cursor": "c2"}}
            return {"data": [{"id": "e2", "traceId": "lf-trace-42"}], "meta": {}}
        if path.startswith("/api/public/traces/"):
            return dict(trace, observations=[o["id"] for o in obs])  # ids only: forces the observations call
        if path == "/api/public/v2/observations":
            raise lf.LangfuseError(404, "not here", path)       # an older server: fall back to v1
        if path == "/api/public/observations":
            return {"data": obs, "meta": {"totalPages": 1}}
        raise AssertionError(path)

    monkeypatch.setattr(lf.LangfuseClient, "get", fake_get)
    client = lf.LangfuseClient("https://lf.example", "pk", "sk")
    assert client.auth == "Basic " + base64.b64encode(b"pk:sk").decode()
    from datetime import datetime, timedelta, timezone
    runs = lf.fetch_runs(client, errors_since=datetime.now(timezone.utc) - timedelta(days=1), failed=True)
    assert [r[0] for r in runs] == ["lf-trace-42"]
    first = calls[0][1]
    assert first["fields"].startswith("core,basic,io") and "fromStartTime" in first and "toStartTime" in first
    assert any(p == "/api/public/observations" and q.get("traceId") == "lf-trace-42" for p, q in calls)
    spans = runs[0][1]
    assert any(s["name"] == "web_search" and s["status"]["code"] == "ERROR" for s in spans)
