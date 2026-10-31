"""ClickHouse span storage, single node or multi-cluster.

``HybridStore`` keeps run metadata, findings, incidents and the rest in the
relational store (SQLite, or Postgres via ``with_clickhouse_spans``) and moves the
high-volume span table to ClickHouse. ``store_from_env`` builds the right combination
from ``TDN_DATABASE_URL`` and ``TDN_CLICKHOUSE``.

Multi-cluster: ``ClusterRouter`` maps each project to a named cluster (for data
residency, e.g. EU tenants on an EU cluster, or to spread load). On a replicated
cluster the DDL uses ``ON CLUSTER``, ``ReplicatedMergeTree`` and a ``Distributed``
table sharded by ``cityHash64(trace_id)`` so one run stays on one shard.

Clients:
* ``HttpClient``: the ClickHouse HTTP interface (port 8123), stdlib only.
* ``ChdbClient``: embedded ClickHouse (``pip install chdb``) for tests and laptops.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional

from ..storage import SQLiteStore


def _q(v: Any) -> str:
    """Quote a value as a ClickHouse literal."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


class HttpClient:
    def __init__(self, url: str = "http://localhost:8123", user: str = "default", password: str = "",
                 timeout: float = 30) -> None:
        self.url, self.user, self.password, self.timeout = url.rstrip("/"), user, password, timeout

    def _post(self, sql: str, body: bytes = b"") -> str:
        params = urllib.parse.urlencode({"query": sql, "output_format_json_quote_64bit_integers": 0})
        req = urllib.request.Request(f"{self.url}/?{params}", data=body, method="POST",
                                     headers={"X-ClickHouse-User": self.user, "X-ClickHouse-Key": self.password})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return r.read().decode()

    def command(self, sql: str) -> None:
        self._post(sql)

    def insert_json(self, table: str, rows: List[Dict[str, Any]]) -> None:
        if rows:
            self._post(f"INSERT INTO {table} FORMAT JSONEachRow", "\n".join(json.dumps(r) for r in rows).encode())

    def query(self, sql: str) -> List[Dict[str, Any]]:
        text = self._post(sql + " FORMAT JSONEachRow")
        return [json.loads(line) for line in text.splitlines() if line.strip()]


class ChdbClient:
    def __init__(self, path: Optional[str] = None) -> None:
        from chdb import session  # optional dependency
        self._s = session.Session(path) if path else session.Session()

    def command(self, sql: str) -> None:
        r = self._s.query(sql)
        if getattr(r, "has_error", lambda: False)():
            raise RuntimeError(r.error_message())

    def insert_json(self, table: str, rows: List[Dict[str, Any]]) -> None:
        if rows:
            self.command(f"INSERT INTO {table} FORMAT JSONEachRow " + "\n".join(json.dumps(r) for r in rows))

    def query(self, sql: str) -> List[Dict[str, Any]]:
        text = str(self._s.query(sql, "JSONEachRow"))
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def close(self) -> None:
        """Shut the embedded engine down now. Left to interpreter exit, chdb's background
        threads can race Python's own shutdown and crash the process (exit code 139)."""
        self._s.close()


def span_ddl(database: str = "tdn", cluster: Optional[str] = None, ttl_days: int = 30) -> List[str]:
    on = f" ON CLUSTER {cluster}" if cluster else ""
    local = "spans_local" if cluster else "spans"
    engine = ("ReplicatedMergeTree('/clickhouse/tables/{shard}/" + database + "/spans_local', '{replica}')"
              if cluster else "MergeTree")
    stmts = [
        f"CREATE DATABASE IF NOT EXISTS {database}{on}",
        f"""CREATE TABLE IF NOT EXISTS {database}.{local}{on} (
            project_id LowCardinality(String), trace_id String, span_id String, parent_span_id String,
            name String, kind LowCardinality(String), step UInt32, agent_name LowCardinality(String),
            start_time DateTime64(9, 'UTC'), end_time DateTime64(9, 'UTC'),
            status_code LowCardinality(String), status_message String,
            model LowCardinality(String), tool_name LowCardinality(String),
            input_tokens UInt32, output_tokens UInt32,
            attributes String, input_digest String, output_digest String
        ) ENGINE = {engine}
        PARTITION BY toDate(start_time)
        ORDER BY (project_id, trace_id, start_time, span_id)
        TTL toDateTime(start_time) + INTERVAL {int(ttl_days)} DAY""",
    ]
    if cluster:
        stmts.append(f"CREATE TABLE IF NOT EXISTS {database}.spans{on} AS {database}.spans_local "
                     f"ENGINE = Distributed({cluster}, {database}, spans_local, cityHash64(trace_id))")
    return stmts


class ClickHouseSpans:
    """Span table operations on one ClickHouse deployment."""

    def __init__(self, client, database: str = "tdn", cluster: Optional[str] = None, ttl_days: int = 30) -> None:
        self.client, self.db = client, database
        for stmt in span_ddl(database, cluster, ttl_days):
            client.command(stmt)

    def insert(self, project: str, spans: Iterable[Dict[str, Any]]) -> int:
        rows = []
        for s in spans:
            a = s.get("attributes", {})
            rows.append({
                "project_id": project, "trace_id": s["trace_id"], "span_id": s["span_id"],
                "parent_span_id": s.get("parent_span_id") or "", "name": s["name"], "kind": s["kind"],
                "step": int(a.get("tdn.step", 0)),
                "agent_name": str(a.get("tdn.agent.name") or a.get("gen_ai.agent.name") or ""),
                "start_time": str(s["start_time"]), "end_time": str(s.get("end_time") or s["start_time"]),
                "status_code": s.get("status", {}).get("code", "UNSET"),
                "status_message": s.get("status", {}).get("message", ""),
                "model": str(a.get("gen_ai.request.model") or ""), "tool_name": str(a.get("gen_ai.tool.name") or ""),
                "input_tokens": int(a.get("gen_ai.usage.input_tokens") or 0),
                "output_tokens": int(a.get("gen_ai.usage.output_tokens") or 0),
                "attributes": json.dumps(a, default=str),
                "input_digest": s.get("input_digest") or "", "output_digest": s.get("output_digest") or "",
            })
        self.client.insert_json(f"{self.db}.spans", rows)
        return len(rows)

    def get(self, trace_id: str) -> List[Dict[str, Any]]:
        rows = self.client.query(
            f"SELECT *, toUnixTimestamp64Nano(start_time) AS st, toUnixTimestamp64Nano(end_time) AS et "
            f"FROM {self.db}.spans WHERE trace_id = {_q(trace_id)} ORDER BY step, start_time")
        out, seen = [], set()
        for r in rows:
            if r["span_id"] in seen:  # an exporter retry may insert a span twice
                continue
            seen.add(r["span_id"])
            out.append({
                "trace_id": r["trace_id"], "span_id": r["span_id"], "parent_span_id": r["parent_span_id"] or None,
                "name": r["name"], "kind": r["kind"], "start_time": int(r["st"]), "end_time": int(r["et"]),
                "status": {"code": r["status_code"], "message": r["status_message"]},
                "attributes": json.loads(r["attributes"] or "{}"),
                "input_digest": r["input_digest"] or None, "output_digest": r["output_digest"] or None,
            })
        return out

    def tool_reliability(self, project: Optional[str] = None, since_ns: Optional[int] = None) -> List[Dict[str, Any]]:
        where = ["kind = 'tool'"]
        if project:
            where.append(f"project_id = {_q(project)}")
        if since_ns:
            where.append(f"start_time >= fromUnixTimestamp64Nano({int(since_ns)})")
        return self.client.query(
            f"SELECT tool_name AS tool, count() AS calls, countIf(status_code = 'ERROR') AS errors, "
            f"round(quantile(0.5)(dateDiff('microsecond', start_time, end_time)) / 1000, 3) AS p50_ms, "
            f"round(quantile(0.95)(dateDiff('microsecond', start_time, end_time)) / 1000, 3) AS p95_ms "
            f"FROM {self.db}.spans WHERE {' AND '.join(where)} GROUP BY tool_name ORDER BY calls DESC")


class ClusterRouter:
    """Route each project to a ClickHouse deployment.

    config = {"clusters": {"us": {"url": "...", "cluster": "tdn_us"}, "eu": {...}},
              "routes": {"acme-eu": "eu"}, "default": "us"}
    """

    def __init__(self, config: Dict[str, Any], client_factory=None) -> None:
        make = client_factory or (lambda c: HttpClient(c["url"], c.get("user", "default"), c.get("password", "")))
        self.routes = dict(config.get("routes", {}))
        self.default = config.get("default") or next(iter(config["clusters"]))
        self.backends = {name: ClickHouseSpans(make(c), c.get("database", "tdn"), c.get("cluster"),
                                               c.get("ttl_days", 30))
                         for name, c in config["clusters"].items()}
        self._trace_home: Dict[str, str] = {}

    def for_project(self, project: str) -> ClickHouseSpans:
        return self.backends[self.routes.get(project, self.default)]

    def insert(self, project: str, spans: List[Dict[str, Any]]) -> int:
        name = self.routes.get(project, self.default)
        for s in spans:
            self._trace_home[s["trace_id"]] = name
        return self.backends[name].insert(project, spans)

    def get(self, trace_id: str) -> List[Dict[str, Any]]:
        home = self._trace_home.get(trace_id)
        if home:
            return self.backends[home].get(trace_id)
        for b in self.backends.values():  # cold lookup: ask each cluster
            rows = b.get(trace_id)
            if rows:
                return rows
        return []


class ClickHouseSpansMixin:
    """Spans in ClickHouse, everything else in the relational store it is mixed into.

    ``_refresh_run`` (the run summary) reads spans through ``self.get_spans``, so it sees
    the ClickHouse copy whichever relational store is underneath.
    """

    router: "ClusterRouter"

    def put_spans(self, project: str, spans: Iterable[Dict[str, Any]]) -> int:
        spans = list(spans)
        self.check_ownership(project, spans)
        n = self.router.insert(project, spans)
        for trace_id in {s["trace_id"] for s in spans}:
            self._refresh_run(project, trace_id)
        return n

    def get_spans(self, trace_id: str) -> List[Dict[str, Any]]:
        return self.router.get(trace_id)

    def tool_stats(self, project: Optional[str] = None, since_ns: Optional[int] = None) -> List[Dict[str, Any]]:
        backends = [self.router.for_project(project)] if project else list(self.router.backends.values())
        merged: Dict[str, Dict[str, Any]] = {}
        for b in backends:
            for r in b.tool_reliability(project, since_ns):
                e = merged.setdefault(r["tool"], {"tool": r["tool"], "calls": 0, "errors": 0, "p50": [], "p95": []})
                e["calls"] += int(r["calls"])
                e["errors"] += int(r["errors"])
                e["p50"].append(float(r["p50_ms"]))
                e["p95"].append(float(r["p95_ms"]))
        return [{"tool": t, "calls": e["calls"], "errors": e["errors"],
                 "error_rate": round(e["errors"] / e["calls"], 4) if e["calls"] else None,
                 # Across clusters this is the worst cluster's percentile, a conservative bound.
                 "latency_ms": {"p50": max(e["p50"]), "p95": max(e["p95"])}}
                for t, e in sorted(merged.items(), key=lambda kv: -kv[1]["calls"])]


class HybridStore(ClickHouseSpansMixin, SQLiteStore):
    """SQLite metadata + ClickHouse spans. Same interface as ``SQLiteStore``."""

    def __init__(self, path: str, router: ClusterRouter) -> None:
        super().__init__(path)
        self.router = router


_HYBRIDS: Dict[type, type] = {}


def with_clickhouse_spans(store, router: ClusterRouter):
    """Move an existing relational store's spans to ClickHouse (e.g. a ``PostgresStore``)."""
    cls = type(store)
    if cls not in _HYBRIDS:
        _HYBRIDS[cls] = type("ClickHouse" + cls.__name__, (ClickHouseSpansMixin, cls), {})
    store.__class__ = _HYBRIDS[cls]
    store.router = router
    return store


def router_from_env() -> Optional[ClusterRouter]:
    """``TDN_CLICKHOUSE`` = JSON router config, or a single URL (``TDN_CLICKHOUSE_CLUSTER`` optional)."""
    import os
    cfg = os.environ.get("TDN_CLICKHOUSE")
    if not cfg:
        return None
    config = json.loads(cfg) if cfg.strip().startswith("{") else \
        {"clusters": {"default": {"url": cfg, "cluster": os.environ.get("TDN_CLICKHOUSE_CLUSTER") or None}}}
    return ClusterRouter(config)


def store_from_env(db_path: str = "agent_tracedown.db"):
    """The store a deployment uses, from the shared environment variables (one implementation,
    used by both the API and ``tracedown-worker``):

    * metadata: ``TDN_DATABASE_URL`` (Postgres) if set, else SQLite at ``db_path``;
    * spans: ClickHouse if ``TDN_CLICKHOUSE`` is set, else the metadata store.
    """
    from . import storage_from_env
    return storage_from_env(db_path) or SQLiteStore(db_path)
