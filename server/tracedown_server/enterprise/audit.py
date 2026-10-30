"""Tamper-evident audit log.

Every entry stores ``hash = sha256(prev_hash + canonical(entry))``. Editing or
deleting any row breaks the chain from that row on, and ``verify()`` reports where.
Recorded: sign-ins, every state-changing API call (who, what, which resource, from
where, result), reads of payload content (prompts and completions may hold personal
data), and policy, key and role changes. Ingest traffic is not logged per call; it
is attributed to the API key in metrics instead.

For stronger guarantees, ship entries to a WORM store (S3 Object Lock) as well;
``export()`` returns them in order with their hashes.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    at         INTEGER NOT NULL,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    resource   TEXT,
    project    TEXT,
    ip         TEXT,
    outcome    TEXT NOT NULL,
    detail     TEXT,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);
"""
GENESIS = "0" * 64


def _canonical(e: Dict[str, Any]) -> str:
    keys = ("at", "actor", "action", "resource", "project", "ip", "outcome", "detail")
    return json.dumps({k: e.get(k) for k in keys}, sort_keys=True, separators=(",", ":"), default=str)


class AuditLog:
    def __init__(self, store) -> None:
        import threading
        self.store = store
        store.ensure_schema(SCHEMA)
        # One lock per store: reading the chain head and appending must be one step.
        self._lock = store.__dict__.setdefault("_audit_lock", threading.Lock())

    def append(self, actor: str, action: str, resource: Optional[str] = None, project: Optional[str] = None,
               ip: Optional[str] = None, outcome: str = "ok", detail: Any = None) -> Dict[str, Any]:
        entry = {"at": time.time_ns(), "actor": actor, "action": action, "resource": resource, "project": project,
                 "ip": ip, "outcome": outcome,
                 "detail": json.dumps(detail, sort_keys=True, default=str) if detail is not None else None}
        # Safe across threads of one process; with several API processes, give the audit log a
        # single writer or a database-side sequence (Postgres: SELECT ... FOR UPDATE).
        with self._lock:
            return self._append(entry, actor, action, resource, project, ip, outcome)

    def _append(self, entry, actor, action, resource, project, ip, outcome) -> Dict[str, Any]:
        last = self.store.query("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1")
        prev = last[0]["hash"] if last else GENESIS
        h = hashlib.sha256((prev + _canonical(entry)).encode()).hexdigest()
        seq = self.store.execute(
            "INSERT INTO audit_log(at, actor, action, resource, project, ip, outcome, detail, prev_hash, hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (entry["at"], actor, action, resource, project, ip, outcome, entry["detail"], prev, h))
        return {**entry, "seq": seq, "prev_hash": prev, "hash": h}

    def list(self, project: Optional[str] = None, actor: Optional[str] = None, limit: int = 100,
             before_seq: Optional[int] = None) -> List[Dict[str, Any]]:
        q, args = "SELECT * FROM audit_log WHERE 1=1", []
        for col, val in (("project", project), ("actor", actor)):
            if val:
                q += f" AND {col}=?"
                args.append(val)
        if before_seq:
            q += " AND seq<?"
            args.append(before_seq)
        rows = self.store.query(q + " ORDER BY seq DESC LIMIT ?", args + [limit])
        for r in rows:
            r["detail"] = json.loads(r["detail"]) if r["detail"] else None
        return rows

    def verify(self) -> Dict[str, Any]:
        prev = GENESIS
        rows = self.store.query("SELECT * FROM audit_log ORDER BY seq")
        for r in rows:
            expect = hashlib.sha256((prev + _canonical(r)).encode()).hexdigest()
            if r["prev_hash"] != prev or r["hash"] != expect:
                return {"ok": False, "entries": len(rows), "broken_at_seq": r["seq"]}
            prev = r["hash"]
        return {"ok": True, "entries": len(rows), "head": prev}

    def export(self) -> List[Dict[str, Any]]:
        return self.store.query("SELECT * FROM audit_log ORDER BY seq")
