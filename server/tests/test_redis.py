"""Redis: shared read cache, live feed across replicas, ingest rate limits (real redis-server)."""
import os
import shutil
import socket
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [os.path.join(ROOT, "sdk", "python"), os.path.join(ROOT, "examples")]

import agent_tracedown as tdn  # noqa: E402
import demo_agents as demo  # noqa: E402
from tracedown_server.api import create_app  # noqa: E402
from tracedown_server.cache import CachedStore, RateLimiter, RedisCache  # noqa: E402
from tracedown_server.storage import SQLiteStore  # noqa: E402

REDIS = shutil.which("redis-server")
pytestmark = pytest.mark.skipif(not REDIS and not os.environ.get("TDN_TEST_REDIS_URL"),
                                reason="needs redis-server or TDN_TEST_REDIS_URL")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def redis_url():
    if os.environ.get("TDN_TEST_REDIS_URL"):
        yield os.environ["TDN_TEST_REDIS_URL"]
        return
    port = _free_port()
    proc = subprocess.Popen([REDIS, "--port", str(port), "--save", "", "--appendonly", "no", "--bind", "127.0.0.1"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import redis
    for _ in range(50):
        try:
            redis.Redis(port=port).ping()
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    yield f"redis://127.0.0.1:{port}/0"
    proc.terminate()
    proc.wait(5)


@pytest.fixture()
def flush(redis_url):
    import redis
    redis.Redis.from_url(redis_url).flushdb()


def _push(client, mem):
    by = {}
    for s in mem.spans:
        by.setdefault(s["trace_id"], []).append(s)
    for spans in by.values():
        client.post("/v1/ingest/blobs", json={"project": "demo", "blobs": mem.blobs})
        r = client.post("/v1/ingest/spans", json={"project": "demo", "spans": spans})
        assert r.status_code in (200, 202), r.text
    mem.clear()


APPS = []


@pytest.fixture(autouse=True)
def _close_buses():
    yield
    while APPS:
        bus = APPS.pop().state.pipeline.bus
        if hasattr(bus, "close"):
            bus.close()


def _two_replicas(redis_url, tmp_path, **env):
    """Two app instances sharing one database file and one Redis, like two server replicas."""
    db = str(tmp_path / "shared.db")
    a = create_app(store=SQLiteStore(db), api_keys=[], queue=False, cache=RedisCache(redis_url))
    b = create_app(store=SQLiteStore(db), api_keys=[], queue=False, cache=RedisCache(redis_url))
    APPS.extend([a, b])
    return a, b


def test_read_cache_hits_and_is_invalidated_by_writes(redis_url, flush):
    mem = tdn.MemoryExporter()
    tdn.init(project="demo", exporter=mem)
    cache = RedisCache(redis_url)
    store = CachedStore(SQLiteStore(":memory:"), cache)
    assert store.list_runs("demo") == []
    assert store.list_runs("demo") == [] and cache.hits >= 1
    try:
        demo.calendar_agent()
    except tdn.ToolError:
        pass
    store.put_blobs(mem.blobs, "demo")
    store.put_spans("demo", mem.spans)
    runs = store.list_runs("demo")
    assert len(runs) == 1 and runs[0]["agent_name"] == "CalendarAssistant"  # the write invalidated the list
    tid = runs[0]["trace_id"]
    assert store.get_findings(tid) is None
    store.put_findings(tid, {"root_cause": {"category": "x"}}, 1)
    assert store.get_findings(tid)["root_cause"]["category"] == "x"
    assert len(store.get_spans(tid)) == len(mem.spans)
    # everything else still reaches the wrapped store
    assert store.trace_project(tid) == "demo" and store.stats("demo")["agents"][0]["runs"] == 1


def test_two_replicas_share_cache_invalidation(redis_url, flush, tmp_path):
    a, b = _two_replicas(redis_url, tmp_path)
    ca, cb = TestClient(a), TestClient(b)
    assert cb.get("/api/v1/runs?project=demo").json()["runs"] == []  # replica B caches the empty list
    mem = tdn.MemoryExporter()
    tdn.init(project="demo", exporter=mem)
    try:
        demo.calendar_agent()
    except tdn.ToolError:
        pass
    _push(ca, mem)  # replica A ingests
    runs = cb.get("/api/v1/runs?project=demo").json()["runs"]
    assert len(runs) == 1 and runs[0]["root_cause"]["category"] == "bad_llm_output"
    detail = cb.get(f"/api/v1/runs/{runs[0]['trace_id']}").json()
    assert detail["findings"]["root_cause"]["step"] == 4
    assert cb.get("/api/v1/analytics?project=demo").status_code == 200


def test_live_feed_crosses_replicas(redis_url, flush, tmp_path):
    a, b = _two_replicas(redis_url, tmp_path)
    sid = b.state.pipeline.bus.subscribe()  # a browser connected to replica B
    mem = tdn.MemoryExporter()
    tdn.init(project="demo", exporter=mem)
    with tdn.run("LiveBot", input="hi") as run:
        tdn.tool_call("t", {"a": 1}, lambda a: a)
        run.output("done")
    _push(TestClient(a), mem)  # spans land on replica A
    got, deadline = [], time.time() + 5
    while time.time() < deadline and not any(e["type"] == "run" for e in got):
        got += b.state.pipeline.bus.drain(sid, timeout=0.5)
    kinds = {e["type"] for e in got}
    assert {"spans", "run"} <= kinds
    assert next(e for e in got if e["type"] == "run")["agent"] == "LiveBot"
    # replica A's own subscribers get each event once, not twice
    sa = a.state.pipeline.bus.subscribe()
    a.state.pipeline.bus.publish({"type": "ping", "project": "demo"})
    seen, deadline = [], time.time() + 3
    while time.time() < deadline and not seen:
        seen = a.state.pipeline.bus.drain(sa, timeout=0.5)
    time.sleep(0.3)
    seen += a.state.pipeline.bus.drain(sa, timeout=0.1)
    assert [e["type"] for e in seen] == ["ping"]


def test_ingest_rate_limit_is_shared_across_replicas(redis_url, flush, tmp_path, monkeypatch):
    monkeypatch.setenv("TDN_INGEST_RATE_LIMIT", "3")
    a, b = _two_replicas(redis_url, tmp_path)
    ca, cb = TestClient(a), TestClient(b)
    body = {"project": "demo", "blobs": {}}
    codes = [c.post("/v1/ingest/blobs", json=body).status_code for c in (ca, cb, ca, cb)]
    assert codes == [200, 200, 200, 429]


def test_in_memory_rate_limit_without_redis():
    rl = RateLimiter(2)
    assert [rl.allow("k"), rl.allow("k"), rl.allow("k"), rl.allow("other")] == [True, True, False, True]
    assert RateLimiter(0).allow("k")


def test_cache_outage_falls_back_to_the_database(redis_url, flush):
    cache = RedisCache(redis_url)
    store = CachedStore(SQLiteStore(":memory:"), cache)

    class Down:
        def __getattr__(self, name):
            def fail(*a, **k):
                raise ConnectionError("redis down")
            return fail
    cache.r = Down()
    assert store.list_runs("demo") == []          # reads still work
    store.put_blobs({}, "demo")                   # pass-through
    cache.bump("demo", "t1")                      # invalidation failure is logged, not raised


def test_no_redis_means_no_cache_and_local_feed(monkeypatch):
    monkeypatch.delenv("TDN_REDIS_URL", raising=False)
    app = create_app(store=SQLiteStore(":memory:"), api_keys=[], queue=False)
    from tracedown_server.pipeline import EventBus
    assert app.state.cache is None and type(app.state.pipeline.bus) is EventBus
    assert not isinstance(app.state.store, CachedStore)
