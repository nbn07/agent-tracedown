"""Regression tests for the v0.6.1 fixes (test report items T-01 to T-18)."""
import json
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [os.path.join(ROOT, "sdk", "python")]

import agent_tracedown as tdn  # noqa: E402
from tracedown_server import api as api_mod  # noqa: E402
from tracedown_server.api import create_app  # noqa: E402
from tracedown_server.demo import agents as demo  # noqa: E402
from tracedown_server.enterprise.pii import Scrubber  # noqa: E402
from tracedown_server.incidents.hooks import _redact  # noqa: E402
from tracedown_server.rca import analyze  # noqa: E402
from tracedown_server.storage import SQLiteStore  # noqa: E402


def _span(tid="t1", sid="s1", **kw):
    return {"trace_id": tid, "span_id": sid, "name": "x", "kind": "agent", "start_time": 1, "end_time": 2,
            "attributes": {"tdn.step": 0}, **kw}


@pytest.fixture()
def client():
    return TestClient(create_app(SQLiteStore(":memory:"), api_keys=[], queue=False))


@pytest.fixture()
def calendar_run(client):
    m = tdn.MemoryExporter()
    tdn.init(project="demo", exporter=m)
    try:
        demo.calendar_agent()
    except tdn.ToolError:
        pass
    client.post("/v1/ingest/blobs", json={"project": "demo", "blobs": m.blobs})
    client.post("/v1/ingest/spans", json={"project": "demo", "spans": m.spans})
    return m.spans[-1]["trace_id"]


# ----------------------------------------------------------------------------- T-01 old page gone

def test_t01_the_old_unescaped_page_is_gone(client, monkeypatch):
    assert client.get("/static/index.html").status_code == 404
    if not api_mod.web_available():
        assert "no web app built in" in client.get("/").text


# ----------------------------------------------------------------------------- read-only (T-02, T-03, T-11)

def test_t02_read_only_allows_only_the_exact_analysis_routes(client, calendar_run):
    client.app.state.read_only = True
    assert client.put("/api/v1/policies/pii/analyze", json={"mode": "redact"}).status_code == 403
    assert client.put("/api/v1/policies/pii/x/replay", json={"mode": "redact"}).status_code == 403
    assert client.post(f"/api/v1/runs/{calendar_run}/analyze").status_code == 200
    assert client.post(f"/api/v1/runs/{calendar_run}/replay", json={"mode": "exact"}).status_code == 200


def test_t03_read_only_hides_admin_pages_and_does_not_audit_refusals(client):
    client.app.state.read_only = True
    for path in ("/api/v1/admin/audit", "/api/v1/admin/users", "/api/v1/admin/keys"):
        assert client.get(path).status_code == 403
    store = client.app.state.store
    before = store.query("SELECT COUNT(*) n FROM audit_log")[0]["n"]
    for _ in range(5):
        client.post("/v1/ingest/spans", json={"spans": [_span()]})
    assert store.query("SELECT COUNT(*) n FROM audit_log")[0]["n"] == before


def test_t11_only_the_loader_gets_in_while_a_read_only_demo_starts(client):
    client.app.state.loading_token = "load-abc"
    assert client.get("/healthz").status_code == 200
    r = client.post("/v1/ingest/spans", json={"spans": [_span()]})
    assert r.status_code == 503 and "starting up" in r.json()["detail"]
    ok = client.post("/v1/ingest/spans", json={"spans": [_span()]}, headers={"Authorization": "Bearer load-abc"})
    assert ok.status_code == 200
    client.app.state.loading_token = None


# ----------------------------------------------------------------------------- T-04 open mode

def test_t04_other_sites_cannot_write_to_an_open_server(client):
    body = json.dumps({"spans": [_span()]})
    h = {"Content-Type": "application/json"}
    evil = client.post("/v1/ingest/spans", content=body, headers={**h, "Origin": "https://evil.example"})
    assert evil.status_code == 403 and "Cross-site" in evil.json()["detail"]
    assert client.post("/v1/ingest/spans", content=body, headers={**h, "Origin": "null"}).status_code == 403
    same = client.post("/v1/ingest/spans", content=body, headers={**h, "Origin": "http://testserver"})
    assert same.status_code == 200
    assert client.post("/v1/ingest/spans", content=body, headers=h).status_code == 200  # SDKs send no Origin


def test_t04_local_dev_server_on_another_port_may_write(client):
    local = TestClient(client.app, base_url="http://127.0.0.1:7070")
    r = local.post("/v1/ingest/spans", json={"spans": [_span()]}, headers={"Origin": "http://localhost:3000"})
    assert r.status_code == 200


def test_t04_dns_rebinding_is_refused_on_a_local_server(client, monkeypatch):
    client.app.state.allowed_hosts = {"127.0.0.1", "localhost", "::1"}
    assert TestClient(client.app, base_url="http://127.0.0.1:7070").get("/api/v1/runs").status_code == 200
    assert TestClient(client.app, base_url="http://[::1]:7070").get("/api/v1/runs").status_code == 200
    r = TestClient(client.app, base_url="http://attacker.example").get("/api/v1/runs")
    assert r.status_code == 421 and "TDN_ALLOWED_HOSTS" in r.json()["detail"]
    monkeypatch.setenv("TDN_ALLOWED_HOSTS", "my-box.lan")
    assert TestClient(client.app, base_url="http://my-box.lan:7070").get("/api/v1/runs").status_code == 200


def test_t04_servers_with_keys_are_not_affected():
    c = TestClient(create_app(SQLiteStore(":memory:"), api_keys=["boot"], queue=False))
    c.app.state.allowed_hosts = {"127.0.0.1"}
    r = c.get("/api/v1/runs", headers={"Authorization": "Bearer boot"})
    assert r.status_code == 200  # Host "testserver": the key is the protection here


# ----------------------------------------------------------------------------- T-05 PII scrubbing

def test_t05_pii_scrubbing_is_fast_on_hostile_input(client):
    s = Scrubber({"mode": "redact"})
    t = time.time()
    s.text("1." * 200_000)
    assert time.time() - t < 2  # was 50 s for 200 KB, longer for more
    assert s.text("write to jane.doe@example.com") == "write to [EMAIL]"
    r = client.put("/api/v1/policies/pii/p", json={"mode": "redact", "custom": [{"name": "x", "pattern": "(a+)+$"}]})
    assert r.status_code == 422 and "exponential" in r.json()["detail"]
    ok = client.put("/api/v1/policies/pii/p", json={"mode": "redact", "custom": [{"name": "order", "pattern": "ORD-\\d{6}"}]})
    assert ok.status_code == 200


# ----------------------------------------------------------------------------- T-06 limits

def test_t06_ingest_limits(client):
    big = client.post("/v1/ingest/blobs", content=b'{"blobs": {"a": "' + b"x" * (21 * 1024 * 1024) + b'"}}',
                      headers={"Content-Type": "application/json"})
    assert big.status_code == 413 and "TDN_MAX_BODY_MB" in big.json()["detail"]
    many = client.post("/v1/ingest/spans", json={"spans": [_span(sid=f"s{i}") for i in range(10_001)]})
    assert many.status_code == 422
    long_name = client.post("/v1/ingest/spans", json={"spans": [_span(name="n" * 2000)]})
    assert long_name.status_code == 422


# ----------------------------------------------------------------------------- T-10 redirect

def test_t10_the_trailing_slash_redirect_stays_on_this_site(tmp_path, monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("home")
    monkeypatch.setattr(api_mod, "WEB", web)
    monkeypatch.delenv("TDN_WEB_DIR", raising=False)
    c = TestClient(create_app(SQLiteStore(":memory:")), follow_redirects=False)
    for path in ("//evil.example.com/x", "/%2fevil.example.com/x", "/\\evil.example.com/x"):
        loc = c.get(path).headers.get("location", "")
        assert loc.startswith("/") and not loc.startswith("//") and not loc.startswith("/\\"), (path, loc)


# ----------------------------------------------------------------------------- T-12, T-13 input validation

def test_t12_limits_must_be_positive(client):
    assert client.get("/api/v1/runs?limit=-1").status_code == 422
    assert client.get("/api/v1/runs?offset=-5").status_code == 422


def test_t13_malformed_input_gets_a_4xx_not_a_500(client):
    bad_spans = [
        _span(attributes={"tdn.step": "three"}),
        _span(attributes={"tdn.step": 0, "gen_ai.usage.input_tokens": "lots"}),
        _span(start_time=10 ** 30),
        _span(name="\ud800"),
    ]
    for s in bad_spans:
        r = client.post("/v1/ingest/spans", content=json.dumps({"spans": [s]}),
                        headers={"Content-Type": "application/json"})
        assert r.status_code == 422, (s, r.status_code, r.text[:200])
    assert client.get("/api/v1/runs?offset=100000000000000000000").status_code == 422
    assert client.get("/api/v1/incidents/100000000000000000000").status_code == 422
    assert client.delete("/api/v1/hooks/100000000000000000000").status_code == 422
    assert client.post("/v1/traces", json={"resourceSpans": "nope"}).status_code == 422
    assert client.post("/v1/traces", json={"resourceSpans": [{"scopeSpans": [{"spans": [5]}]}]}).status_code == 422
    deep = "[" * 100_000 + "]" * 100_000
    assert client.post("/api/v1/import", content=deep).status_code == 422
    assert client.post("/api/v1/hooks", json={"kind": "webhook", "config": {"url": 5}}).status_code == 422
    assert client.get("/healthz").status_code == 200


# ----------------------------------------------------------------------------- T-14 webhook headers

def test_t14_custom_webhook_headers_are_masked():
    shown = _redact({"url": "https://example.com/hook", "headers": {"X-Sig": "supersecretvalue123"}})
    assert "supersecret" not in json.dumps(shown) and shown["headers"]["X-Sig"].startswith("•••")


# ----------------------------------------------------------------------------- T-07, T-16 diagnosis and demo

def test_t07_a_fixed_demo_replay_is_a_success():
    m = tdn.MemoryExporter()
    tdn.init(project="demo", exporter=m)
    demo.refund_agent()
    original = m.spans[-1]["trace_id"]
    bundle = {"spans": m.trace(original), "blobs": m.blobs}
    with tdn.replay(bundle, mode="from-step", from_step=2, overrides={"retrieval_drop": {"stale": True, "steps": [2]}}):
        demo.refund_agent()
    root = next(s for s in m.spans if s["trace_id"] != original and not s["parent_span_id"])
    assert root["attributes"]["tdn.run.outcome"] == "success"


@pytest.mark.parametrize("message,category", [
    ("RuntimeError: service down", "tool_failure"),
    ("payment API is down for maintenance", "tool_failure"),
    ("card declined: insufficient funds", "planning_failure"),  # the system said no: unchanged
])
def test_t16_outages_are_tool_failures_and_refusals_are_not(message, category):
    m = tdn.MemoryExporter()
    tdn.init(project="x", exporter=m)
    try:
        with tdn.run("Q", input="x") as run:
            tdn.tool_call("pay", {}, lambda: (_ for _ in ()).throw(RuntimeError(message)))
    except RuntimeError:
        pass
    assert analyze(m.trace(run.trace_id), m.blobs)["root_cause"]["category"] == category


# ----------------------------------------------------------------------------- from the security re-check

def test_custom_pii_patterns_cannot_hang_the_server():
    """A pattern's text cannot reveal every slow pattern, so custom ones run with a time limit and
    fail closed: text that was not fully scanned is not passed on."""
    for pattern, text in [("(a|a)+$", "a" * 40 + "!"), ("(a|aa)+$", "a" * 60 + "!")]:
        s = Scrubber({"mode": "redact", "custom": [{"name": "x", "pattern": pattern}]})
        t = time.time()
        out = [s.text(text) for _ in range(5)]
        assert time.time() - t < 1.5, pattern       # one time-out, then the pattern is skipped
        assert all(o == "[X:SCAN_TIMEOUT]" for o in out) and "a" * 10 not in "".join(out)
    ok = Scrubber({"mode": "redact", "custom": [{"name": "order", "pattern": "ORD-\\d{6}"}]})
    assert ok.text("see ORD-123456 and mail a@b.co") == "see [ORDER] and mail [EMAIL]"


def test_open_tracedown_server_on_localhost_refuses_other_host_names(monkeypatch):
    monkeypatch.setenv("TDN_BIND_HOST", "127.0.0.1")
    app = create_app(SQLiteStore(":memory:"), api_keys=[], queue=False)
    assert TestClient(app, base_url="http://attacker.example:7206").get("/api/v1/runs").status_code == 421
    assert TestClient(app, base_url="http://localhost:7206").get("/api/v1/runs").status_code == 200
    odd = TestClient(app, base_url="http://localhost:7206").get("/api/v1/runs", headers={"Host": "localhost:7201.evil.example"})
    assert odd.status_code == 421
    monkeypatch.setenv("TDN_BIND_HOST", "0.0.0.0")  # deliberately exposed: no host check (Origin still is)
    app = create_app(SQLiteStore(":memory:"), api_keys=[], queue=False)
    assert TestClient(app, base_url="http://my-box.lan").get("/api/v1/runs").status_code == 200


def test_read_only_reads_do_not_write_audit_rows(client, calendar_run):
    client.app.state.read_only = True
    store = client.app.state.store
    before = store.query("SELECT COUNT(*) n FROM audit_log")[0]["n"]
    bundle = client.get(f"/api/v1/runs/{calendar_run}/bundle").json()
    digest = next(s["output_digest"] for s in bundle["spans"] if s.get("output_digest"))
    client.get(f"/api/v1/blobs/{digest}?trace_id={calendar_run}")
    assert store.query("SELECT COUNT(*) n FROM audit_log")[0]["n"] == before
