"""v2.0: RBAC, OIDC SSO, audit log, PII policies, ClickHouse (embedded via chdb),
in-process and Kafka queues."""
import base64
import json
import time
import urllib.parse

import pytest
from fastapi.testclient import TestClient

import agent_tracedown as tdn
import demo_agents as demo
from tracedown_server.api import create_app
from tracedown_server.enterprise.audit import AuditLog
from tracedown_server.enterprise.pii import Scrubber
from tracedown_server.enterprise.queue import InProcessQueue, KafkaQueue, KafkaWorker
from tracedown_server.enterprise.sso import OIDC
from tracedown_server.pipeline import Pipeline
from tracedown_server.storage import SQLiteStore


@pytest.fixture()
def mem():
    m = tdn.MemoryExporter()
    tdn.init(project="demo", exporter=m)
    return m


def push(client, mem, project="demo", headers=None):
    client.post("/v1/ingest/blobs", json={"project": project, "blobs": mem.blobs}, headers=headers)
    r = client.post("/v1/ingest/spans", json={"project": project, "spans": mem.spans}, headers=headers)
    if r.status_code < 300:
        mem.clear()
    return r


def calendar(mem):
    try:
        demo.calendar_agent()
    except tdn.ToolError:
        pass
    return mem.spans[-1]["trace_id"]


# ----------------------------------------------------------------------------- RBAC

def test_rbac_keys_roles_and_project_isolation(mem):
    app = create_app(store=SQLiteStore(":memory:"), api_keys=["tdn_bootstrap"], queue=False)
    c = TestClient(app)
    boot = {"Authorization": "Bearer tdn_bootstrap"}
    ingest_a = c.post("/api/v1/admin/keys", json={"project": "team-a", "role": "ingest"}, headers=boot).json()["key"]
    viewer_a = c.post("/api/v1/admin/keys", json={"project": "team-a", "role": "viewer"}, headers=boot).json()["key"]
    admin_b = c.post("/api/v1/admin/keys", json={"project": "team-b", "role": "admin"}, headers=boot).json()["key"]
    H = lambda k: {"Authorization": f"Bearer {k}"}  # noqa: E731

    assert c.get("/api/v1/runs").status_code == 401
    tid_a = calendar(mem)
    assert push(c, mem, "team-a", H(ingest_a)).status_code == 200
    tid_b = calendar(mem)
    assert push(c, mem, "team-b", H(ingest_a)).status_code == 403          # ingest key is scoped to team-a
    assert push(c, mem, "team-b", H(admin_b)).status_code == 200

    assert c.get("/api/v1/runs", headers=H(ingest_a)).status_code == 403   # ingest keys cannot read
    runs = c.get("/api/v1/runs", headers=H(viewer_a)).json()["runs"]
    assert {r["trace_id"] for r in runs} == {tid_a}                         # team-b is invisible
    assert c.get(f"/api/v1/runs/{tid_b}", headers=H(viewer_a)).status_code == 403
    assert c.post(f"/api/v1/runs/{tid_a}/analyze", headers=H(viewer_a)).status_code == 403  # viewer: read only
    assert c.post(f"/api/v1/runs/{tid_b}/analyze", headers=H(admin_b)).status_code == 200
    assert c.get("/api/v1/admin/audit", headers=H(admin_b)).status_code == 403  # org owners only
    assert c.get("/api/v1/admin/audit", headers=boot).json()["integrity"]["ok"]

    key_id = c.get("/api/v1/admin/keys?project=team-a", headers=boot).json()["keys"][1]["id"]
    c.delete(f"/api/v1/admin/keys/{key_id}", headers=boot)
    assert c.get("/api/v1/runs", headers=H(viewer_a)).status_code == 401    # revoked


def test_open_mode_closes_once_a_key_exists(mem):
    c = TestClient(create_app(store=SQLiteStore(":memory:"), api_keys=[], queue=False))
    assert c.get("/api/v1/me").json()["open_mode"] is True
    key = c.post("/api/v1/admin/keys", json={"project": "p", "role": "admin"}).json()["key"]
    assert c.get("/api/v1/runs").status_code == 401
    assert c.get("/api/v1/runs", headers={"Authorization": f"Bearer {key}"}).status_code == 200


# ----------------------------------------------------------------------------- SSO

def _idp():
    """A fake OIDC provider: RSA key pair, discovery doc, JWKS, token endpoint."""
    jwt = pytest.importorskip("jwt")
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="k1", alg="RS256", use="sig")
    issuer = "https://idp.example.com"
    state = {"nonce": None, "claims": {}}

    def fetch(url, form=None):
        if url.endswith("/.well-known/openid-configuration"):
            return {"issuer": issuer, "authorization_endpoint": f"{issuer}/authorize",
                    "token_endpoint": f"{issuer}/token", "jwks_uri": f"{issuer}/jwks"}
        if url.endswith("/jwks"):
            return {"keys": [jwk]}
        if url.endswith("/token"):
            assert form["grant_type"] == "authorization_code" and form["code_verifier"]
            now = int(time.time())
            claims = {"iss": issuer, "aud": "tdn-client", "sub": "00u1", "iat": now, "exp": now + 300,
                      "nonce": state["nonce"], "email": "naveen@acme.com", "email_verified": True,
                      "name": "Naveen", "groups": ["support-eng"], **state["claims"]}
            return {"id_token": jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})}
        raise AssertionError(url)

    oidc = OIDC(issuer, "tdn-client", "secret", "https://tdn.acme.com/auth/oidc/callback",
                role_map={"support-eng": {"support-bot": "developer"}, "tdn-owners": "owner"},
                allowed_domains=["acme.com"], fetch=fetch)
    return oidc, state


def _login(client, state):
    r = client.get("/auth/oidc/login", follow_redirects=False)
    assert r.status_code == 302
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(r.headers["location"]).query))
    assert q["code_challenge_method"] == "S256"
    state["nonce"] = q["nonce"]
    return client.get(f"/auth/oidc/callback?code=abc&state={q['state']}", follow_redirects=False)


def test_oidc_login_maps_groups_to_roles(mem):
    oidc, state = _idp()
    store = SQLiteStore(":memory:")
    app = create_app(store=store, api_keys=["tdn_boot"], oidc=oidc, queue=False)
    c = TestClient(app)
    cb = _login(c, state)
    assert cb.status_code == 302 and "tdn_session" in cb.cookies
    me = c.get("/api/v1/me").json()
    assert me["email"] == "naveen@acme.com" and me["roles"] == {"support-bot": "developer"}
    assert c.get("/api/v1/runs?project=support-bot").status_code == 200
    assert c.get("/api/v1/runs?project=other").status_code == 403
    entries = c.get("/api/v1/admin/audit", headers={"Authorization": "Bearer tdn_boot"}).json()["entries"]
    assert any(e["action"] == "sso login" and e["actor"] == "naveen@acme.com" for e in entries)


def test_oidc_rejects_bad_tokens(mem):
    oidc, state = _idp()
    c = TestClient(create_app(store=SQLiteStore(":memory:"), api_keys=["tdn_boot"], oidc=oidc, queue=False))
    state["claims"] = {"aud": "someone-else"}
    assert _login(c, state).status_code == 401
    state["claims"] = {"email": "eve@evil.com"}
    assert _login(c, state).status_code == 401
    state["claims"] = {"exp": int(time.time()) - 3600}
    assert _login(c, state).status_code == 401
    assert c.get("/auth/oidc/callback?code=x&state=forged").status_code == 401
    # a nonce that does not match the login request
    r = c.get("/auth/oidc/login", follow_redirects=False)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(r.headers["location"]).query))
    state.update(nonce="replayed", claims={})
    assert c.get(f"/auth/oidc/callback?code=abc&state={q['state']}").status_code == 401


def test_session_tamper_is_rejected(mem):
    oidc, state = _idp()
    c = TestClient(create_app(store=SQLiteStore(":memory:"), api_keys=["tdn_boot"], oidc=oidc, queue=False))
    token = _login(c, state).cookies["tdn_session"]
    head, body, sig = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=="))
    claims["sub"] = "999"
    forged = head + "." + base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode() + "." + sig
    c.cookies.clear()
    c.cookies.set("tdn_session", forged)
    assert c.get("/api/v1/me").status_code == 401


# ----------------------------------------------------------------------------- audit log

def test_audit_chain_detects_tampering():
    store = SQLiteStore(":memory:")
    log = AuditLog(store)
    for i in range(5):
        log.append("alice", f"action {i}", resource=f"r{i}")
    assert log.verify() == {"ok": True, "entries": 5, "head": log.export()[-1]["hash"]}
    store.execute("UPDATE audit_log SET actor='mallory' WHERE seq=3")
    v = log.verify()
    assert v["ok"] is False and v["broken_at_seq"] == 3


def test_payload_reads_and_mutations_are_audited(mem):
    c = TestClient(create_app(store=SQLiteStore(":memory:"), api_keys=["tdn_boot"], queue=False))
    H = {"Authorization": "Bearer tdn_boot"}
    tid = calendar(mem)
    push(c, mem, headers=H)
    c.get(f"/api/v1/runs/{tid}/bundle", headers=H)
    c.post("/api/v1/hooks", json={"kind": "webhook", "config": {"url": "https://x.test"}}, headers=H)
    actions = [e["action"] for e in c.get("/api/v1/admin/audit", headers=H).json()["entries"]]
    assert "read payloads (bundle)" in actions and "POST /api/v1/hooks" in actions


# ----------------------------------------------------------------------------- PII policies

def test_scrubber_detectors():
    s = Scrubber({"mode": "redact", "custom": [{"name": "employee_id", "pattern": r"EMP-\d{6}"}]})
    out = s.text("Mail ana@acme.com, card 4111 1111 1111 1111, not-a-card 1234 5678 9012 3456, "
                 "ssn 123-45-6789, ip 10.0.0.12, key sk-abcdefghijklmnopqrstuv, EMP-004211")
    assert "[EMAIL]" in out and "[CREDIT_CARD]" in out and "1234 5678 9012 3456" in out  # Luhn check
    assert "[US_SSN]" in out and "[IP_ADDRESS]" in out and "[API_KEY]" in out and "[EMPLOYEE_ID]" in out
    h = Scrubber({"mode": "hash"})
    assert h.text("ana@acme.com") == h.text("ana@acme.com") != Scrubber({"mode": "hash"}).text("bob@acme.com")


def test_pii_policy_applies_at_ingest(mem):
    app = create_app(store=SQLiteStore(":memory:"), api_keys=[], queue=False)
    c = TestClient(app)
    assert c.put("/api/v1/policies/pii/demo", json={"mode": "redact", "allow_fields": ["model"]}).status_code == 200
    with tdn.run("Bot", input="I am ana@acme.com, card 4111-1111-1111-1111") as run:
        run.output("ok")
    push(c, mem)
    allblobs = " ".join(r["content"] for r in app.state.store.query("SELECT content FROM payloads"))
    assert "ana@acme.com" not in allblobs and "[EMAIL]" in allblobs and "[CREDIT_CARD]" in allblobs
    stats = c.get("/api/v1/policies/pii/demo").json()["redactions"]
    assert {r["detector"] for r in stats} >= {"email", "credit_card"}
    assert c.put("/api/v1/policies/pii/demo", json={"mode": "shred"}).status_code == 422


# ----------------------------------------------------------------------------- ClickHouse (embedded)

def test_clickhouse_hybrid_store_with_two_clusters(mem):
    pytest.importorskip("chdb")
    from tracedown_server.enterprise.clickhouse import ChdbClient, ClusterRouter, HybridStore

    client = ChdbClient()  # one embedded engine, two databases standing in for two clusters
    router = ClusterRouter({"clusters": {"us": {"database": "tdn_us"}, "eu": {"database": "tdn_eu"}},
                            "routes": {"acme-eu": "eu"}, "default": "us"}, client_factory=lambda cfg: client)
    store = HybridStore(":memory:", router)
    c = TestClient(create_app(store=store, api_keys=[], queue=False))
    tid_us = calendar(mem)
    push(c, mem, "acme-us")
    tid_eu = calendar(mem)
    push(c, mem, "acme-eu")
    assert client.query(f"SELECT count() n FROM tdn_eu.spans WHERE trace_id='{tid_eu}'")[0]["n"] == 6
    assert client.query(f"SELECT count() n FROM tdn_us.spans WHERE trace_id='{tid_eu}'")[0]["n"] == 0
    router._trace_home.clear()  # cold lookup path
    run = c.get(f"/api/v1/runs/{tid_eu}").json()
    assert len(run["spans"]) == 6 and run["findings"]["root_cause"]["category"] == "bad_llm_output"
    assert c.get(f"/api/v1/runs/{tid_us}/stacktrace").text.startswith("AgentFailureException")
    tools = store.tool_stats(None, None)
    assert tools[0]["tool"] == "calendar_create_event" and tools[0]["calls"] == 2 and tools[0]["errors"] == 2


def test_clickhouse_cluster_ddl_uses_replication():
    from tracedown_server.enterprise.clickhouse import span_ddl
    ddl = "\n".join(span_ddl("tdn", cluster="prod"))
    assert "ON CLUSTER prod" in ddl and "ReplicatedMergeTree" in ddl and "Distributed(prod, tdn, spans_local" in ddl


# ----------------------------------------------------------------------------- queues

def test_inprocess_queue_ingest_returns_202_then_processes(mem):
    store = SQLiteStore(":memory:")
    pipe_holder = {}
    q = InProcessQueue(lambda project, spans: pipe_holder["p"].process_spans(project, spans))
    app = create_app(store=store, api_keys=[], queue=q)
    pipe_holder["p"] = app.state.pipeline
    c = TestClient(app)
    tid = calendar(mem)
    r = push(c, mem)
    assert r.status_code == 202 and r.json()["queued"]
    q.flush()
    assert c.get(f"/api/v1/runs/{tid}").json()["findings"]["root_cause"]["category"] == "bad_llm_output"
    assert q.errors == []


class FakeMsg:
    def __init__(self, key, value):
        self._k, self._v = key, value

    def key(self):
        return self._k

    def value(self):
        return self._v

    def error(self):
        return None


class FakeBroker:
    """Stands in for confluent_kafka Producer/Consumer: one topic, keyed partitions."""

    def __init__(self, partitions=3):
        self.parts = [[] for _ in range(partitions)]
        self.committed = []

    # producer API (a real producer's poll() only serves delivery callbacks)
    def produce(self, topic, key, value):
        self.parts[hash(key) % len(self.parts)].append(FakeMsg(key, value))

    def producer(self):
        broker = self

        class P:
            produce = staticmethod(broker.produce)

            @staticmethod
            def poll(timeout=0):
                return 0

            @staticmethod
            def flush(timeout=None):
                return 0
        return P()

    def poll(self, timeout=0):
        for p in self.parts:
            if p:
                return p.pop(0)
        return None

    def flush(self, timeout=None):
        return 0

    # consumer API
    def subscribe(self, topics):
        self.topics = topics

    def commit(self, message, asynchronous=False):
        self.committed.append(message)

    def seek(self, message):  # real consumers take a TopicPartition; the double takes the message
        self.parts[hash(message.key()) % len(self.parts)].insert(0, message)


def test_kafka_producer_and_worker_with_fake_broker(mem):
    broker = FakeBroker()
    store = SQLiteStore(":memory:")
    pipe = Pipeline(store)
    producer = KafkaQueue("unused:9092", producer=broker.producer())
    tids = [calendar(mem) for _ in range(3)]
    producer.publish("demo", mem.spans)
    store.put_blobs(mem.blobs, "demo")
    # every run went to exactly one partition, spans in order with the root last
    for p in broker.parts:
        by_trace = {}
        for m in p:
            by_trace.setdefault(m.key(), []).append(json.loads(m.value()))
        for msgs in by_trace.values():
            assert len(msgs) == 1 and msgs[0]["spans"][-1]["parent_span_id"] is None
    worker = KafkaWorker("unused:9092", pipe.process_spans, consumer=broker)
    while worker.poll_once():
        pass
    assert worker.processed == 3 and len(broker.committed) == 3
    assert all(store.get_findings(t)["root_cause"]["category"] == "bad_llm_output" for t in tids)


def test_kafka_worker_does_not_commit_on_failure():
    broker = FakeBroker()
    broker.produce("tdn.spans", b"t1", json.dumps({"project": "p", "spans": []}).encode())

    def boom(project, spans):
        raise RuntimeError("db down")

    worker = KafkaWorker("x", boom, consumer=broker)
    with pytest.raises(RuntimeError):
        worker.poll_once()
    assert broker.committed == [] and worker.failed == 1
    # the message was rewound, so a healthy handler gets it on the next poll
    seen = []
    worker.handler = lambda project, spans: seen.append(project)
    assert worker.poll_once() and seen == ["p"] and len(broker.committed) == 1


def test_sso_callback_requires_the_browser_that_started_login(mem):
    oidc, state = _idp()
    app = create_app(store=SQLiteStore(":memory:"), api_keys=["tdn_boot"], oidc=oidc, queue=False)
    attacker, victim = TestClient(app), TestClient(app)
    r = attacker.get("/auth/oidc/login", follow_redirects=False)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(r.headers["location"]).query))
    state["nonce"] = q["nonce"]
    # the victim opens the attacker's callback URL: no matching state cookie in the victim's browser
    assert victim.get(f"/auth/oidc/callback?code=abc&state={q['state']}", follow_redirects=False).status_code == 401


def test_removing_idp_group_revokes_access(mem):
    oidc, state = _idp()
    c = TestClient(create_app(store=SQLiteStore(":memory:"), api_keys=["tdn_boot"], oidc=oidc, queue=False))
    state["claims"] = {"groups": ["support-eng", "tdn-owners"]}
    _login(c, state)
    assert c.get("/api/v1/me").json()["org_role"] == "owner"
    state["claims"] = {"groups": []}
    c.cookies.clear()
    _login(c, state)
    me = c.get("/api/v1/me").json()
    assert me["org_role"] == "member" and me["roles"] == {}
    assert c.get("/api/v1/runs?project=support-bot").status_code == 403
