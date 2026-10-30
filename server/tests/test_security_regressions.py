"""Regression tests for the independent security and correctness review (v0.2)."""
import hashlib
import json
import threading

import pytest
from fastapi.testclient import TestClient

import agent_tracedown as tdn
import demo_agents as demo
from tracedown_server.api import create_app
from tracedown_server.enterprise.audit import AuditLog
from tracedown_server.incidents import hooks
from tracedown_server.storage import SQLiteStore


@pytest.fixture()
def mem():
    m = tdn.MemoryExporter()
    tdn.init(project="demo", exporter=m)
    return m


@pytest.fixture()
def two_projects(mem):
    sink = []
    app = create_app(store=SQLiteStore(":memory:"), api_keys=["tdn_boot"], queue=False,
                     notifier_transport=lambda url, headers, body: (sink.append((url, body)), (200, "ok"))[1])
    c = TestClient(app)
    boot = {"Authorization": "Bearer tdn_boot"}
    keys = {}
    for proj in ("A", "B"):
        for role in ("admin", "viewer"):
            keys[(proj, role)] = {"Authorization": "Bearer " + c.post(
                "/api/v1/admin/keys", json={"project": proj, "role": role}, headers=boot).json()["key"]}
    return c, keys, boot, sink, app


def push(c, mem, project, headers):
    c.post("/v1/ingest/blobs", json={"project": project, "blobs": mem.blobs}, headers=headers)
    r = c.post("/v1/ingest/spans", json={"project": project, "spans": mem.spans}, headers=headers)
    tid = mem.spans[-1]["trace_id"] if mem.spans else None
    mem.clear()
    return r, tid


def calendar():
    try:
        demo.calendar_agent()
    except tdn.ToolError:
        pass


def test_project_admin_cannot_create_or_see_global_hooks(two_projects):
    c, keys, boot, sink, app = two_projects
    r = c.post("/api/v1/hooks", json={"kind": "webhook", "config": {"url": "https://evil.example/a"}},
               headers=keys[("A", "admin")])
    assert r.status_code == 403  # no project = global = owners only
    c.post("/api/v1/hooks", json={"kind": "webhook", "project": "B", "name": "b-hook",
                                  "config": {"url": "https://b.example/h", "headers": {"Authorization": "Bearer B-SECRET"}}},
           headers=keys[("B", "admin")])
    listed = c.get("/api/v1/hooks", headers=keys[("A", "viewer")]).json()
    assert listed["hooks"] == [] and listed["deliveries"] == []
    own = c.get("/api/v1/hooks", headers=keys[("B", "viewer")]).json()["hooks"]
    assert own and "B-SECRET" not in json.dumps(own)
    gid = c.post("/api/v1/hooks", json={"kind": "webhook", "config": {"url": "https://ops.example/h"}},
                 headers=boot).json()["id"]
    assert c.delete(f"/api/v1/hooks/{gid}", headers=keys[("A", "admin")]).status_code == 403


def test_blob_must_belong_to_the_named_run(two_projects, mem):
    c, keys, boot, *_ = two_projects
    calendar()
    _, tid_a = push(c, mem, "A", keys[("A", "admin")])
    with tdn.run("Secret", input="B's secret prompt") as run:
        run.output("x")
    _, tid_b = push(c, mem, "B", keys[("B", "admin")])
    b_input = c.get(f"/api/v1/runs/{tid_b}", headers=boot).json()["spans"][-1]["input_digest"] or \
        next(s["input_digest"] for s in c.get(f"/api/v1/runs/{tid_b}", headers=boot).json()["spans"] if s["input_digest"])
    r = c.get(f"/api/v1/blobs/{b_input}?trace_id={tid_a}", headers=keys[("A", "viewer")])
    assert r.status_code == 404
    assert c.get(f"/api/v1/blobs/{b_input}", headers=keys[("A", "viewer")]).status_code == 422  # trace_id required


def test_another_project_cannot_write_into_a_trace(two_projects, mem):
    c, keys, *_ = two_projects
    calendar()
    _, tid_b = push(c, mem, "B", keys[("B", "admin")])
    with tdn.run("Hijack") as run:
        pass
    spans = mem.spans
    for s in spans:
        s["trace_id"] = tid_b
    r = c.post("/v1/ingest/spans", json={"project": "A", "spans": spans}, headers=keys[("A", "admin")])
    assert r.status_code == 409
    assert c.get(f"/api/v1/runs/{tid_b}", headers=keys[("B", "viewer")]).status_code == 200
    assert c.get(f"/api/v1/runs/{tid_b}", headers=keys[("A", "viewer")]).status_code == 403


def test_unfinished_runs_keep_their_project(two_projects, mem):
    c, keys, *_ = two_projects
    tdn.init(project="B", exporter=mem, stream=True)
    with tdn.run("Streaming") as run:
        tdn.tool_call("t", {"a": 1}, lambda a: a)
        r = c.post("/v1/ingest/spans", json={"project": "B", "spans": mem.spans}, headers=keys[("B", "admin")])
        assert r.status_code == 200
        assert c.get(f"/api/v1/runs/{run.trace_id}", headers=keys[("A", "viewer")]).status_code == 403
        assert c.post(f"/api/v1/runs/{run.trace_id}/feedback", json={"score": 1},
                      headers=keys[("B", "admin")]).status_code == 409  # not finished: no 500


def test_cross_project_aggregates_need_a_project(two_projects, mem):
    c, keys, boot, *_ = two_projects
    for path in ("/api/v1/metrics/agents", "/metrics", "/api/v1/analytics",
                 "/api/v1/drift/values?agent=X&dimension=model",
                 "/api/v1/drift/compare?agent=X&dimension=model&a=1&b=2"):
        assert c.get(path, headers=keys[("A", "viewer")]).status_code == 400, path
        assert c.get(path + ("&" if "?" in path else "?") + "project=B", headers=keys[("A", "viewer")]).status_code == 403
    assert c.post("/api/v1/drift/scan?agent=X", headers=keys[("A", "admin")]).status_code == 403
    assert c.put("/api/v1/settings/prices", json={"m": {"input": 1, "output": 1}},
                 headers=keys[("A", "admin")]).status_code == 403
    assert c.get("/metrics", headers=boot).status_code == 200


def test_pii_policy_cannot_be_bypassed_by_another_projects_upload(two_projects, mem):
    c, keys, boot, *_ = two_projects
    c.put("/api/v1/policies/pii/B", json={"mode": "redact"}, headers=keys[("B", "admin")])
    content = json.dumps("contact bob@corp.com")
    digest = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
    assert c.post("/v1/ingest/blobs", json={"project": "A", "blobs": {digest: content}},
                  headers=keys[("A", "admin")]).status_code == 200
    c.post("/v1/ingest/blobs", json={"project": "B", "blobs": {digest: content}}, headers=keys[("B", "admin")])
    stored = c.app.state.store.get_blobs([digest], "B")[digest]
    assert "bob@corp.com" not in stored and "[EMAIL]" in stored
    forged = c.post("/v1/ingest/blobs", json={"project": "A", "blobs": {digest: json.dumps("planted")}},
                    headers=keys[("A", "admin")])
    assert forged.status_code == 422


def test_audit_chain_survives_concurrent_writers():
    log = AuditLog(SQLiteStore(":memory:"))
    threads = [threading.Thread(target=lambda: [log.append("t", "x") for _ in range(25)]) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert log.verify()["ok"] and log.verify()["entries"] == 200


def test_drift_reports_do_not_collide_across_projects(two_projects, mem):
    c, keys, boot, *_ = two_projects
    for proj in ("A", "B"):
        tdn.init(project=proj, exporter=mem)
        for v in ("billing@v3", "billing@v4"):
            for i in range(20):
                demo.support_cohort(v, i)
        push(c, mem, proj, keys[(proj, "admin")])
    reps = c.get("/api/v1/drift/reports", headers=boot).json()["reports"]
    assert {r["project"] for r in reps if r["dimension"] == "prompt_version"} == {"A", "B"}
    assert {r["project"] for r in c.get("/api/v1/drift/reports", headers=keys[("A", "viewer")]).json()["reports"]} == {"A"}


def test_hook_urls_to_private_addresses_are_refused():
    store = SQLiteStore(":memory:")
    store.ensure_schema(hooks.SCHEMA)
    for url in ("http://169.254.169.254/latest/meta-data", "https://127.0.0.1/x", "https://localhost/x", "ftp://x.example"):
        with pytest.raises(ValueError):
            hooks.create(store, "webhook", {"url": url})
    with pytest.raises(ValueError):
        hooks.create(store, "github", {"repo": "a/b", "token": "t", "api_url": "https://10.0.0.5"})


def test_failed_delivery_does_not_echo_response_body():
    from tracedown_server import incidents
    store = SQLiteStore(":memory:")
    store.ensure_schema(hooks.SCHEMA + incidents.SCHEMA)
    hooks.create(store, "webhook", {"url": "https://h.example/x"}, min_severity="low")
    n = hooks.Notifier(store, transport=lambda u, h, b: (500, "internal secret stack trace"))
    res = n.notify("incident.opened", {"id": 1, "severity": "high", "title": "t", "run_count": 1, "root_cause": "r"})
    assert res[0]["error"] == "HTTP 500" and "secret" not in json.dumps(hooks.deliveries(store))


def test_exact_replay_refuses_unrecorded_steps(mem):
    with tdn.run("Bot") as run:
        tdn.llm_call(lambda p, **_: "a", prompt="q", model="m")
    bundle = {"spans": mem.trace(run.trace_id), "blobs": dict(mem.blobs)}
    charged = []
    with tdn.replay(bundle, mode="exact"), pytest.raises(tdn.ReplayDivergence):
        with tdn.run("Bot"):
            tdn.tool_call("charge_card", {"usd": 10}, lambda usd: charged.append(usd))
    assert charged == []
