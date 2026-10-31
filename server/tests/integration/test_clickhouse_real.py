"""ClickHouse span storage against real ClickHouse servers (HTTP interface).

Skipped unless ``TDN_IT_CLICKHOUSE_URLS`` names two servers, e.g.

    TDN_IT_CLICKHOUSE_URLS=http://127.0.0.1:18123,http://127.0.0.1:18124 pytest server/tests/integration

Any ClickHouse HTTP endpoint works: ``clickhouse-server``, ClickHouse Cloud, or
``chdb_http.py`` in this folder (the real ClickHouse engine behind a minimal HTTP front).
"""
import os
import time
import uuid

import pytest

URLS = [u for u in os.environ.get("TDN_IT_CLICKHOUSE_URLS", "").split(",") if u]
pytestmark = pytest.mark.skipif(len(URLS) < 2, reason="set TDN_IT_CLICKHOUSE_URLS to two ClickHouse HTTP URLs")


def _span(trace, i, project_tool="search", err=False, start=None, kind="tool"):
    t = start or time.time_ns()
    return {"trace_id": trace, "span_id": f"{trace[:8]}{i:08x}", "parent_span_id": None if i == 0 else f"{trace[:8]}{0:08x}",
            "name": f"execute_tool {project_tool}" if kind == "tool" else "invoke_agent Bot", "kind": kind,
            "start_time": t + i * 1_000_000, "end_time": t + i * 1_000_000 + 3_000_000,
            "status": {"code": "ERROR" if err else "OK", "message": "boom" if err else ""},
            "attributes": {"tdn.step": i, "gen_ai.tool.name": project_tool} if kind == "tool" else
                          {"tdn.step": 0, "gen_ai.agent.name": "Bot", "tdn.run.root": True},
            "input_digest": None, "output_digest": None}


def _run(trace, n=4, err_at=None, start=None):
    spans = [_span(trace, i, err=(i == err_at), start=start) for i in range(1, n)]
    return spans + [_span(trace, 0, kind="agent", start=start)]


@pytest.fixture
def router():
    from tracedown_server.enterprise.clickhouse import ClusterRouter
    db = "it_" + uuid.uuid4().hex[:8]
    cfg = {"clusters": {"us": {"url": URLS[0], "database": db}, "eu": {"url": URLS[1], "database": db}},
           "routes": {"acme-eu": "eu"}, "default": "us"}
    return cfg, ClusterRouter(cfg)


def test_projects_are_routed_to_their_cluster_and_read_back(router, tmp_path):
    from tracedown_server.enterprise.clickhouse import HybridStore, HttpClient
    cfg, r = router
    store = HybridStore(str(tmp_path / "meta.db"), r)
    us, eu = uuid.uuid4().hex, uuid.uuid4().hex
    store.put_spans("acme-us", _run(us))
    store.put_spans("acme-eu", _run(eu, err_at=2))
    assert len(store.get_spans(us)) == 4 and len(store.get_spans(eu)) == 4
    db = cfg["clusters"]["us"]["database"]
    in_us = HttpClient(URLS[0]).query(f"SELECT count() AS n FROM {db}.spans WHERE trace_id = '{eu}'")[0]["n"]
    in_eu = HttpClient(URLS[1]).query(f"SELECT count() AS n FROM {db}.spans WHERE trace_id = '{eu}'")[0]["n"]
    assert (int(in_us), int(in_eu)) == (0, 4), "EU data must stay on the EU cluster"
    run = store.get_run(eu)
    assert run is not None and run["agent_name"] == "Bot" and run["project"] == "acme-eu"


def test_a_fresh_process_finds_traces_without_the_routing_cache(router):
    from tracedown_server.enterprise.clickhouse import ClusterRouter
    cfg, r = router
    t = uuid.uuid4().hex
    r.insert("acme-eu", _run(t))
    fresh = ClusterRouter(cfg)  # another API replica: no memory of where t went
    assert [s["span_id"] for s in fresh.get(t)] == [s["span_id"] for s in r.get(t)]


def test_redelivered_spans_are_read_once(router):
    _, r = router
    t = uuid.uuid4().hex
    spans = _run(t)
    r.insert("acme-us", spans)
    r.insert("acme-us", spans)  # a Kafka redelivery or exporter retry
    assert len(r.get(t)) == len(spans)


def test_ttl_drops_expired_partitions(router):
    from tracedown_server.enterprise.clickhouse import ClickHouseSpans, HttpClient
    db = "it_ttl_" + uuid.uuid4().hex[:8]
    c = HttpClient(URLS[0])
    s = ClickHouseSpans(c, database=db, ttl_days=30)
    old, new = uuid.uuid4().hex, uuid.uuid4().hex
    s.insert("p", _run(old, start=time.time_ns() - 40 * 86400 * 10**9))
    s.insert("p", _run(new))
    c.command(f"OPTIMIZE TABLE {db}.spans FINAL")
    assert s.get(old) == [] and len(s.get(new)) == 4


def test_tool_reliability_merges_both_clusters(router, tmp_path):
    from tracedown_server.enterprise.clickhouse import HybridStore
    _, r = router
    store = HybridStore(str(tmp_path / "meta.db"), r)
    store.put_spans("acme-us", _run(uuid.uuid4().hex, n=5, err_at=1))
    store.put_spans("acme-eu", _run(uuid.uuid4().hex, n=5))
    stats = {x["tool"]: x for x in store.tool_stats()}
    assert stats["search"]["calls"] == 8 and stats["search"]["errors"] == 1
    assert stats["search"]["latency_ms"]["p95"] >= 2.9


@pytest.mark.skipif(not os.environ.get("TDN_IT_DATABASE_URL"), reason="set TDN_IT_DATABASE_URL (Postgres) too")
def test_postgres_metadata_with_clickhouse_spans_runs_the_full_pipeline(router):
    """The production layout: runs and findings in Postgres, spans in ClickHouse."""
    from tracedown_server.enterprise.clickhouse import with_clickhouse_spans
    from tracedown_server.enterprise.queue import build_pipeline
    from tracedown_server.storage.postgres import PostgresStore
    _, r = router
    store = with_clickhouse_spans(PostgresStore(os.environ["TDN_IT_DATABASE_URL"],
                                                schema="it_" + uuid.uuid4().hex[:10]), r)
    try:
        t = uuid.uuid4().hex
        result = build_pipeline(store).process_spans("acme-eu", _run(t, err_at=2))
        assert result["accepted"] == 4 and t in result["analyzed"]
        assert store.get_run(t)["outcome"] == "failure" and len(store.get_spans(t)) == 4
        assert store.get_findings(t)["root_cause"]["category"] in ("tool_failure", "loop")  # 3 identical search calls
    finally:
        store.close()
