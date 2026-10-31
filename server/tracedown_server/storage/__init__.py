"""Storage behind one interface.

``SQLiteStore`` is the dev profile: one file, zero services. The production
profile (Postgres for runs/findings, ClickHouse for spans, S3 for blobs)
implements the same methods; DDL lives in ``deploy/sql/``.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    trace_id      TEXT PRIMARY KEY,
    project       TEXT NOT NULL,
    agent_name    TEXT,
    outcome       TEXT,
    error         TEXT,
    started_at    INTEGER,
    duration_ms   REAL,
    span_count    INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    models        TEXT,
    replay_of     TEXT,
    attributes    TEXT,
    profile       TEXT
);
CREATE INDEX IF NOT EXISTS runs_project_time ON runs(project, started_at DESC);
CREATE TABLE IF NOT EXISTS spans (
    project        TEXT NOT NULL,
    trace_id       TEXT NOT NULL,
    span_id        TEXT NOT NULL,
    parent_span_id TEXT,
    name           TEXT,
    kind           TEXT,
    start_time     INTEGER,
    end_time       INTEGER,
    status_code    TEXT,
    status_message TEXT,
    attributes     TEXT,
    input_digest   TEXT,
    output_digest  TEXT,
    PRIMARY KEY (trace_id, span_id)
);
CREATE TABLE IF NOT EXISTS payloads (
    project  TEXT NOT NULL,
    digest   TEXT NOT NULL,
    content  TEXT NOT NULL,
    PRIMARY KEY (project, digest)
);
-- A trace belongs to the project that sent its first span; others cannot write into it.
CREATE TABLE IF NOT EXISTS trace_owner (
    trace_id  TEXT PRIMARY KEY,
    project   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
    trace_id   TEXT PRIMARY KEY,
    report     TEXT NOT NULL,
    created_at INTEGER
);
"""


class TraceOwnershipError(ValueError):
    """Spans for a trace that another project already owns."""


class SQLiteStore:
    def __init__(self, path: str = ":memory:") -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(runs)")}
            if "profile" not in cols:  # migrate v0.1 databases
                self._db.execute("ALTER TABLE runs ADD COLUMN profile TEXT")

    # ------------------------------------------------------------------ generic access for feature modules
    def ensure_schema(self, ddl: str) -> None:
        with self._lock:
            self._db.executescript(ddl)

    def execute(self, sql: str, args: Iterable[Any] = ()) -> Optional[int]:
        """Run one statement; for an INSERT, return the new row id (None if nothing was inserted)."""
        with self._lock, self._db:
            cur = self._db.execute(sql, tuple(args))
            if cur.rowcount == 0 and sql.lstrip()[:6].upper() == "INSERT":
                return None  # ON CONFLICT DO NOTHING skipped it; lastrowid would be stale
            return cur.lastrowid

    def query(self, sql: str, args: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, tuple(args)).fetchall()]

    # ------------------------------------------------------------------ writes
    def put_blobs(self, blobs: Dict[str, str], project: str = "default") -> int:
        """Payloads are stored per project, so one project's content (or its PII policy) never
        decides what another project reads under the same digest."""
        with self._lock, self._db:
            self._db.executemany("INSERT OR IGNORE INTO payloads(project, digest, content) VALUES (?, ?, ?)",
                                 [(project, d, c) for d, c in blobs.items()])
        return len(blobs)

    def claim_traces(self, project: str, trace_ids: Iterable[str]) -> List[str]:
        """Record ownership of new traces; return the ids already owned by another project."""
        ids = sorted(set(trace_ids))
        with self._lock, self._db:
            self._db.executemany("INSERT OR IGNORE INTO trace_owner VALUES (?, ?)", [(t, project) for t in ids])
            rows = self._db.execute(f"SELECT trace_id, project FROM trace_owner WHERE trace_id IN "
                                    f"({','.join('?' * len(ids))})", ids).fetchall() if ids else []
        return [r["trace_id"] for r in rows if r["project"] != project]

    def check_ownership(self, project: str, spans: List[Dict[str, Any]]) -> None:
        taken = self.claim_traces(project, (sp["trace_id"] for sp in spans))
        if taken:
            raise TraceOwnershipError(f"trace(s) {', '.join(t[:8] for t in taken)} belong to another project")

    def trace_project(self, trace_id: str) -> Optional[str]:
        rows = self.query("SELECT project FROM trace_owner WHERE trace_id=?", (trace_id,))
        return rows[0]["project"] if rows else None

    def put_spans(self, project: str, spans: Iterable[Dict[str, Any]]) -> int:
        spans = list(spans)
        self.check_ownership(project, spans)
        rows = [(
            project, s["trace_id"], s["span_id"], s.get("parent_span_id"), s["name"], s["kind"],
            s["start_time"], s.get("end_time"), s.get("status", {}).get("code", "UNSET"),
            s.get("status", {}).get("message", ""), json.dumps(s.get("attributes", {}), default=str),
            s.get("input_digest"), s.get("output_digest"),
        ) for s in spans]
        with self._lock, self._db:
            self._db.executemany("INSERT OR REPLACE INTO spans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        for trace_id in {r[1] for r in rows}:
            self._refresh_run(project, trace_id)
        return len(rows)

    def _refresh_run(self, project: str, trace_id: str) -> None:
        spans = self.get_spans(trace_id)
        roots = [s for s in spans if not s["parent_span_id"]]
        if not roots:
            return  # root arrives last; run row is written then
        root = roots[0]
        a = root["attributes"]
        llm = [s for s in spans if s["kind"] == "llm"]
        tokens_in = sum(int(s["attributes"].get("gen_ai.usage.input_tokens") or 0) for s in llm)
        tokens_out = sum(int(s["attributes"].get("gen_ai.usage.output_tokens") or 0) for s in llm)
        models = sorted({s["attributes"].get("gen_ai.request.model") for s in llm} - {None})
        errors = [s for s in spans if s["status"]["code"] == "ERROR"]
        outcome = a.get("tdn.run.outcome") or ("failure" if errors else "success")
        first_err = root["status"]["message"] or (errors[0]["status"]["message"] if errors else None)
        duration = ((root["end_time"] or root["start_time"]) - root["start_time"]) / 1e6
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "(SELECT profile FROM runs WHERE trace_id=?))",
                (trace_id, project, a.get("gen_ai.agent.name", root["name"]), outcome, first_err,
                 root["start_time"], duration, len(spans), tokens_in, tokens_out, json.dumps(models),
                 a.get("tdn.replay.of"), json.dumps(a, default=str), trace_id))

    def set_profile(self, trace_id: str, profile: Dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET profile=? WHERE trace_id=?", (json.dumps(profile, default=str), trace_id))

    def run_profiles(self, agent: Optional[str] = None, project: Optional[str] = None,
                     since_ns: Optional[int] = None) -> List[Dict[str, Any]]:
        q, args = "SELECT trace_id, project, agent_name, outcome, started_at, profile FROM runs " \
                  "WHERE profile IS NOT NULL AND replay_of IS NULL", []
        for col, val in (("agent_name", agent), ("project", project)):
            if val:
                q += f" AND {col}=?"
                args.append(val)
        if since_ns:
            q += " AND started_at>=?"
            args.append(since_ns)
        rows = self.query(q + " ORDER BY started_at", args)
        for r in rows:
            r["profile"] = json.loads(r["profile"])
        return rows

    def put_findings(self, trace_id: str, report: Dict[str, Any], created_at: int) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO findings VALUES (?,?,?)",
                             (trace_id, json.dumps(report, default=str), created_at))

    # ------------------------------------------------------------------ reads
    @staticmethod
    def _span_row(r: sqlite3.Row) -> Dict[str, Any]:
        return {
            "trace_id": r["trace_id"], "span_id": r["span_id"], "parent_span_id": r["parent_span_id"],
            "name": r["name"], "kind": r["kind"], "start_time": r["start_time"], "end_time": r["end_time"],
            "status": {"code": r["status_code"], "message": r["status_message"]},
            "attributes": json.loads(r["attributes"] or "{}"),
            "input_digest": r["input_digest"], "output_digest": r["output_digest"],
        }

    @staticmethod
    def _run_row(r: sqlite3.Row) -> Dict[str, Any]:
        d = dict(r)
        d["models"] = json.loads(d["models"] or "[]")
        d["attributes"] = json.loads(d["attributes"] or "{}")
        d["profile"] = json.loads(d["profile"]) if d.get("profile") else None
        return d

    def get_spans(self, trace_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM spans WHERE trace_id=? ORDER BY start_time, span_id", (trace_id,)).fetchall()
        return [self._span_row(r) for r in rows]

    def get_run(self, trace_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            r = self._db.execute("SELECT * FROM runs WHERE trace_id=?", (trace_id,)).fetchone()
        return self._run_row(r) if r else None

    def list_runs(self, project: Optional[str] = None, agent: Optional[str] = None,
                  outcome: Optional[str] = None, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        q, args = "SELECT * FROM runs WHERE 1=1", []
        for col, val in (("project", project), ("agent_name", agent), ("outcome", outcome)):
            if val:
                q += f" AND {col}=?"
                args.append(val)
        q += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [self._run_row(r) for r in rows]

    def get_blobs(self, digests: Iterable[str], project: str = "default") -> Dict[str, str]:
        digests = [d for d in set(digests) if d]
        if not digests:
            return {}
        with self._lock:
            rows = self._db.execute(
                f"SELECT digest, content FROM payloads WHERE project=? AND digest IN ({','.join('?' * len(digests))})",
                [project] + digests).fetchall()
        return {r["digest"]: r["content"] for r in rows}

    def get_findings(self, trace_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            r = self._db.execute("SELECT report FROM findings WHERE trace_id=?", (trace_id,)).fetchone()
        return json.loads(r["report"]) if r else None

    def tool_stats(self, project: Optional[str] = None, since_ns: Optional[int] = None) -> List[Dict[str, Any]]:
        """Per tool: calls, errors, error rate, latency p50/p95 (ms)."""
        q = ("SELECT json_extract(attributes, '$.\"gen_ai.tool.name\"') AS tool, status_code, "
             "(COALESCE(end_time, start_time) - start_time) / 1e6 AS ms FROM spans WHERE kind='tool'")
        args: list = []
        if project:
            q += " AND project=?"
            args.append(project)
        if since_ns:
            q += " AND start_time>=?"
            args.append(since_ns)
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for r in self.query(q, args):
            groups.setdefault(r["tool"] or "unknown", []).append(r)
        return [_tool_row(tool, [r["ms"] for r in rows], sum(r["status_code"] == "ERROR" for r in rows))
                for tool, rows in sorted(groups.items(), key=lambda kv: -len(kv[1]))]

    def stats(self, project: Optional[str] = None) -> Dict[str, Any]:
        q = ("SELECT agent_name, COUNT(*) n, SUM(CASE WHEN outcome='failure' THEN 1 ELSE 0 END) failures, AVG(duration_ms) avg_ms, "
             "SUM(input_tokens) tin, SUM(output_tokens) tout FROM runs")
        args: list = []
        if project:
            q += " WHERE project=?"
            args.append(project)
        q += " GROUP BY agent_name ORDER BY n DESC"
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return {"agents": [{
            "agent": r["agent_name"], "runs": r["n"], "failures": r["failures"],
            "success_rate": round(1 - (r["failures"] or 0) / r["n"], 4) if r["n"] else None,
            "avg_duration_ms": round(r["avg_ms"] or 0, 1),
            "input_tokens": r["tin"] or 0, "output_tokens": r["tout"] or 0,
        } for r in rows]}


def _tool_row(tool: str, latencies: List[float], errors: int) -> Dict[str, Any]:
    xs = sorted(latencies)

    def pct(q):
        if not xs:
            return None
        k = (len(xs) - 1) * q
        lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
        return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 3)

    return {"tool": tool, "calls": len(xs), "errors": errors,
            "error_rate": round(errors / len(xs), 4) if xs else None,
            "latency_ms": {"p50": pct(0.5), "p95": pct(0.95)}}
