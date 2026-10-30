"""Incidents (v1.3): many runs, one root cause.

Each analysed run with a root cause gets a **signature**:
``category | agent | target | normalized title`` where *target* is the tool, retrieval
source, model or memory key involved and the title has quoted values, numbers and ids
replaced. Runs sharing a signature are one incident, so "132 affected runs" is a
count, not a guess.

An incident keeps a timeline of events (opened, escalated, notified, resolved,
regressed) and of affected runs per hour, a rule-based suggested fix, and drives the
notification hooks in ``tracedown_server.incidents.hooks``.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project        TEXT,
    signature      TEXT NOT NULL,
    agent          TEXT,
    category       TEXT NOT NULL,
    target         TEXT,
    title          TEXT NOT NULL,
    root_cause     TEXT NOT NULL,
    suggested_fix  TEXT,
    status         TEXT NOT NULL DEFAULT 'open',
    severity       TEXT NOT NULL DEFAULT 'low',
    run_count      INTEGER NOT NULL DEFAULT 0,
    first_seen     INTEGER NOT NULL,
    last_seen      INTEGER NOT NULL,
    resolved_at    INTEGER,
    UNIQUE (project, signature)
);
CREATE TABLE IF NOT EXISTS incident_runs (
    incident_id  INTEGER NOT NULL,
    trace_id     TEXT NOT NULL,
    at           INTEGER NOT NULL,
    PRIMARY KEY (incident_id, trace_id)
);
CREATE TABLE IF NOT EXISTS incident_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id  INTEGER NOT NULL,
    at           INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    detail       TEXT
);
"""

SEVERITY_ORDER = ["low", "medium", "high", "critical"]
# Categories that hurt users silently (the run "succeeded") start one level higher.
SILENT = {"hallucination", "stale_source"}


# ----------------------------------------------------------------------------- signature

_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_HEX = re.compile(r"\b[0-9a-f]{8,}\b")
_NUM = re.compile(r"\b\d+(?:\.\d+)?\b")


def normalize(text: str) -> str:
    text = _QUOTED.sub("<v>", text)
    text = _HEX.sub("<id>", text)
    return _NUM.sub("<n>", text).strip()


def target_of(finding: Dict[str, Any], span: Optional[Dict[str, Any]]) -> Optional[str]:
    if span is None:
        return None
    a = span["attributes"]
    if span["kind"] == "tool":
        return a.get("gen_ai.tool.name")
    if span["kind"] == "retrieval":
        stale = [c.get("source") for c in a.get("tdn.retrieval.chunks", []) if c.get("stale")]
        return stale[0] if stale else a.get("tdn.retrieval.source")
    if span["kind"] == "llm":
        return a.get("gen_ai.request.model")
    if span["kind"] == "memory":
        return a.get("tdn.memory.key")
    if span["kind"] == "agent":
        return None  # the run as a whole: no narrower target
    return span["name"]


def signature(report: Dict[str, Any], spans_by_id: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    rc = report.get("root_cause")
    if not rc:
        return None
    span = spans_by_id.get(rc["span_id"])
    target = target_of(rc, span)
    agent = (span or {}).get("attributes", {}).get("tdn.agent.name") or report.get("agent")
    sig = " | ".join([rc["category"], str(agent), str(target), normalize(rc["title"])])
    return {"signature": sig, "category": rc["category"], "agent": agent, "target": target,
            "title": rc["title"], "label": rc.get("label", rc["category"])}


# ----------------------------------------------------------------------------- suggested fixes

def suggest_fix(category: str, target: Optional[str], finding: Dict[str, Any]) -> str:
    t = target or "the component"
    ev = " ".join(finding.get("evidence", []))
    m = re.search(r"which (\w+)\(\)", finding.get("title", ""))
    consumer = f"{m.group(1)}()" if m else "the next step"
    fixes = {
        "stale_source": f"Remove or re-index {t}: it is marked superseded but still ranks first. Add a version "
                        f"filter to the retriever so superseded documents are excluded.",
        "retrieval_failure": f"Check the {t} index for the query terms in the evidence; add documents or a "
                             f"fallback answer when the best score is below threshold.",
        "bad_llm_output": f"Output from {t} is passed to {consumer} unvalidated. Constrain the output (JSON schema / "
                          f"structured output), state the expected format in the prompt, or normalize the value "
                          f"before the call. Then replay a failing run from the LLM step to confirm.",
        "invalid_tool_args": f"Arguments to {t} fail its schema. Give the model the schema as a function "
                             f"definition and validate before calling.",
        "tool_failure": f"{t} is failing. Check its availability and error budget; add retries with backoff for "
                        f"transient errors and a user-facing fallback.",
        "loop": f"The agent repeats {t} with identical arguments. Add a stop condition: dedupe identical calls, "
                f"cap retries, and tell the model when a call returned nothing.",
        "context_window": "Prompts are near the context limit. Summarize or trim history and retrieved chunks, "
                          "or move to a model with a larger window.",
        "hallucination": "Answers contain claims that no source supports. Require citations for numbers, lower "
                         "temperature, and add a post-answer check against tool results"
                         + (" (the answer contradicts a tool result)" if "contradicted" in ev else "") + ".",
        "memory_corruption": f"Memory key {t} reads back a different value than was written. Check concurrent "
                             f"writers and serialization of that key.",
        "planning_failure": "The planner picks an option whose precondition failed. Make the precondition an "
                            "explicit check before the decision step.",
        "wrong_tool": f"The agent called {t} where another tool fits the request. Clarify tool descriptions and "
                      f"add negative examples to the system prompt.",
        "unhandled_error": "An exception escapes the agent. Catch it at the step boundary and record the step "
                           "that raised it.",
        "reported_failure": "Runs are reported as failed without a detected cause. Add retrieval/tool "
                            "instrumentation around the steps before the answer so the engine has evidence.",
    }
    return fixes.get(category, "Inspect the root-cause step in an affected run and replay it with a fix.")


# ----------------------------------------------------------------------------- persistence

def _severity(category: str, run_count: int, recent_hour: int) -> str:
    level = 0 if run_count < 5 else 1 if run_count < 20 else 2
    if recent_hour >= 50:
        level = 3
    if category in SILENT:
        level = min(level + 1, 3)
    return SEVERITY_ORDER[level]


def _event(store, incident_id: int, kind: str, detail: Any = None) -> None:
    store.execute("INSERT INTO incident_events(incident_id, at, kind, detail) VALUES (?,?,?,?)",
                  (incident_id, time.time_ns(), kind, json.dumps(detail, default=str) if detail is not None else None))


def record(store, project: str, report: Dict[str, Any], spans: List[Dict[str, Any]],
           at_ns: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Attach an analysed run to its incident. Returns {"incident", "events"} or None."""
    if not report.get("root_cause"):
        return None
    sig = signature(report, {s["span_id"]: s for s in spans})
    if sig is None:
        return None
    at = at_ns or time.time_ns()
    existing = store.query("SELECT * FROM incidents WHERE project IS ? AND signature=?", (project, sig["signature"]))
    events: List[str] = []
    if not existing:
        iid = store.execute(
            "INSERT INTO incidents(project, signature, agent, category, target, title, root_cause, suggested_fix, "
            "status, severity, run_count, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project, sig["signature"], sig["agent"], sig["category"], sig["target"],
             f"{sig['agent']}: {sig['label']}" + (f" in {sig['target']}" if sig["target"] else ""),
             sig["title"], suggest_fix(sig["category"], sig["target"], report["root_cause"]),
             "open", _severity(sig["category"], 1, 1), 0, at, at))
        _event(store, iid, "opened", {"trace_id": report["trace_id"]})
        events.append("incident.opened")
    else:
        iid = existing[0]["id"]
        if existing[0]["status"] == "resolved":
            store.execute("UPDATE incidents SET status='open', resolved_at=NULL WHERE id=?", (iid,))
            _event(store, iid, "regressed", {"trace_id": report["trace_id"]})
            events.append("incident.regressed")
    store.execute("INSERT OR IGNORE INTO incident_runs(incident_id, trace_id, at) VALUES (?,?,?)",
                             (iid, report["trace_id"], at))
    count = store.query("SELECT COUNT(*) n FROM incident_runs WHERE incident_id=?", (iid,))[0]["n"]
    hour = store.query("SELECT COUNT(*) n FROM incident_runs WHERE incident_id=? AND at>=?",
                       (iid, at - 3600 * 10 ** 9))[0]["n"]
    inc = store.query("SELECT * FROM incidents WHERE id=?", (iid,))[0]
    sev = _severity(inc["category"], count, hour)
    if SEVERITY_ORDER.index(sev) > SEVERITY_ORDER.index(inc["severity"]):
        _event(store, iid, "escalated", {"from": inc["severity"], "to": sev, "runs": count})
        events.append("incident.escalated")
    else:
        sev = inc["severity"]
    store.execute("UPDATE incidents SET run_count=?, last_seen=MAX(last_seen, ?), severity=? WHERE id=?",
                  (count, at, sev, iid))
    return {"incident": get(store, iid, with_timeline=False), "events": events}


def set_status(store, incident_id: int, status: str, actor: str = "user") -> Dict[str, Any]:
    if status not in ("open", "acknowledged", "resolved"):
        raise ValueError("status must be open, acknowledged or resolved")
    store.execute("UPDATE incidents SET status=?, resolved_at=? WHERE id=?",
                  (status, time.time_ns() if status == "resolved" else None, incident_id))
    _event(store, incident_id, status, {"by": actor})
    return get(store, incident_id)


def list_incidents(store, project: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
    q, args = "SELECT * FROM incidents WHERE 1=1", []
    if project:
        q += " AND project=?"
        args.append(project)
    if status:
        q += " AND status=?"
        args.append(status)
    return store.query(q + " ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
                           "WHEN 'medium' THEN 2 ELSE 3 END, last_seen DESC", args)


def get(store, incident_id: int, with_timeline: bool = True) -> Optional[Dict[str, Any]]:
    rows = store.query("SELECT * FROM incidents WHERE id=?", (incident_id,))
    if not rows:
        return None
    inc = rows[0]
    if not with_timeline:
        return inc
    runs = store.query("SELECT trace_id, at FROM incident_runs WHERE incident_id=? ORDER BY at", (incident_id,))
    events = store.query("SELECT at, kind, detail FROM incident_events WHERE incident_id=? ORDER BY at, id",
                         (incident_id,))
    for e in events:
        e["detail"] = json.loads(e["detail"]) if e["detail"] else None
    per_hour = Counter((r["at"] // (3600 * 10 ** 9)) * 3600 for r in runs)
    inc["timeline"] = {
        "events": events,
        "runs_per_hour": [{"hour_unix": h, "runs": n} for h, n in sorted(per_hour.items())],
    }
    inc["affected_runs"] = [r["trace_id"] for r in runs[-50:]]
    return inc
