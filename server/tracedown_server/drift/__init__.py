"""Drift detection (v1.2): did a change to the prompt, model, tools or knowledge base
change how the agent behaves?

Every finished run gets a *profile*: the versions it ran with (dimensions) and what it
did (features). A drift report compares two cohorts of runs that differ in one
dimension, e.g. ``prompt_version`` ``extract@v3`` vs ``extract@v4``:

* success rate, with a two-proportion z-test;
* tool-call distribution, with Jensen-Shannon divergence and per-tool rate changes;
* tool error rates, steps, tokens and latency;
* the mix of root-cause categories among failures.

``scan()`` runs automatically: when a dimension takes a new value (a deploy) and both
the new and the previous value have enough runs, a report is written and, if it shows
drift, sent to the incident hooks.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter
from typing import Any, Dict, List, Optional

DIMENSIONS = ("prompt_version", "model", "tool_schema", "kb_snapshot", "release")

SCHEMA = """
CREATE TABLE IF NOT EXISTS drift_reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT,
    agent       TEXT NOT NULL,
    dimension   TEXT NOT NULL,
    baseline    TEXT NOT NULL,
    candidate   TEXT NOT NULL,
    verdict     TEXT NOT NULL,
    severity    TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    report      TEXT NOT NULL,
    UNIQUE (project, agent, dimension, baseline, candidate)
);
"""


def _short(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:10]


# ----------------------------------------------------------------------------- profiles

def build_profile(spans: List[Dict[str, Any]], findings: Optional[Dict[str, Any]] = None,
                  claims: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    root = next(s for s in spans if not s["parent_span_id"])
    ra = root["attributes"]
    ordered = sorted(spans, key=lambda s: s["attributes"].get("tdn.step", 0))
    llm = [s for s in ordered if s["kind"] == "llm"]
    tools = [s for s in ordered if s["kind"] == "tool"]
    retr = [s for s in ordered if s["kind"] == "retrieval"]

    prompts = sorted({s["attributes"].get("tdn.prompt.version") for s in llm} - {None})
    schemas = {s["attributes"].get("gen_ai.tool.name", s["name"]): s["attributes"].get("tdn.tool.schema_hash")
               for s in tools if s["attributes"].get("tdn.tool.schema_hash")}
    snapshots = sorted({s["attributes"].get("tdn.kb.snapshot") for s in retr} - {None})
    if not snapshots and retr:  # derive a snapshot id from the chunk sources and versions seen
        seen = sorted({f"{c.get('source')}@{c.get('version')}" for s in retr
                       for c in s["attributes"].get("tdn.retrieval.chunks", []) if c.get("version")})
        snapshots = [f"derived:{_short(seen)}"] if seen else []

    dims = {
        "prompt_version": ra.get("tdn.prompt_version") or ("+".join(prompts) if prompts else None),
        "model": "+".join(sorted({s["attributes"].get("gen_ai.request.model") for s in llm} - {None})) or None,
        "tool_schema": f"schema:{_short(schemas)}" if schemas else None,
        "kb_snapshot": ra.get("tdn.kb_snapshot") or ("+".join(snapshots) if snapshots else None),
        "release": ra.get("tdn.release"),
    }
    tool_calls = Counter(s["attributes"].get("gen_ai.tool.name", s["name"]) for s in tools)
    tool_errors = Counter(s["attributes"].get("gen_ai.tool.name", s["name"])
                          for s in tools if s["status"]["code"] == "ERROR")
    tokens = sum(int(s["attributes"].get("gen_ai.usage.input_tokens") or 0) +
                 int(s["attributes"].get("gen_ai.usage.output_tokens") or 0) for s in llm)
    outcome = ra.get("tdn.run.outcome") or ("failure" if root["status"]["code"] == "ERROR" else "success")
    return {
        "dims": dims,
        "tool_schemas": schemas,
        "features": {
            "outcome": outcome,
            "tool_calls": dict(tool_calls),
            "tool_errors": dict(tool_errors),
            "tool_sequence": [s["attributes"].get("gen_ai.tool.name", s["name"]) for s in tools],
            "steps": len(spans) - 1,
            "tokens": tokens,
            "duration_ms": ((root["end_time"] or root["start_time"]) - root["start_time"]) / 1e6,
            "root_cause": ((findings or {}).get("root_cause") or {}).get("category"),
            "categories": sorted({f["category"] for f in (findings or {}).get("findings", [])}),
            "claims": (claims or {}).get("summary"),
            "retrieval": _retrieval_usage(retr, findings),
            "llm": [{"model": s["attributes"].get("gen_ai.request.model"),
                     "input_tokens": int(s["attributes"].get("gen_ai.usage.input_tokens") or 0),
                     "output_tokens": int(s["attributes"].get("gen_ai.usage.output_tokens") or 0),
                     "cost_usd": s["attributes"].get("tdn.llm.cost_usd")} for s in llm],
            "feedback": ra.get("tdn.feedback.score"),
        },
    }


def _retrieval_usage(retr: List[Dict[str, Any]], findings: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per retrieval step: how many chunks, best score, stale share, and whether its
    output reached the final answer (from the RCA data-flow graph)."""
    used = set()
    flow = (findings or {}).get("dataflow") or {}
    frontier = list(flow.get("__final_answer__", []))
    while frontier:
        cur = frontier.pop()
        if cur not in used:
            used.add(cur)
            frontier += flow.get(cur, [])
    out = []
    for s in retr:
        a = s["attributes"]
        chunks = a.get("tdn.retrieval.chunks") or []
        out.append({"source": a.get("tdn.retrieval.source") or "retriever",
                    "chunks": a.get("tdn.retrieval.count", len(chunks)),
                    "max_score": a.get("tdn.retrieval.max_score"),
                    "stale": sum(1 for c in chunks if c.get("stale")),
                    "used": (s["span_id"] in used) if findings is not None else None})
    return out


# ----------------------------------------------------------------------------- statistics

def _z_test(s1: int, n1: int, s2: int, n2: int) -> Optional[float]:
    """Two-sided p-value for a difference between two proportions (normal approximation)."""
    if not n1 or not n2:
        return None
    p = (s1 + s2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    z = (s1 / n1 - s2 / n2) / se
    return math.erfc(abs(z) / math.sqrt(2))


def _jsd(p: Dict[str, float], q: Dict[str, float]) -> float:
    """Jensen-Shannon divergence (base 2, 0 = identical, 1 = disjoint)."""
    keys = set(p) | set(q)
    sp, sq = sum(p.values()) or 1, sum(q.values()) or 1
    P = {k: p.get(k, 0) / sp for k in keys}
    Q = {k: q.get(k, 0) / sq for k in keys}
    M = {k: (P[k] + Q[k]) / 2 for k in keys}

    def kl(a, b):
        return sum(a[k] * math.log2(a[k] / b[k]) for k in keys if a[k] > 0)

    return round((kl(P, M) + kl(Q, M)) / 2, 4)


def _mean(xs: List[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def _change(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or a == 0:
        return None
    return round((b - a) / a, 4)


# ----------------------------------------------------------------------------- compare

def compare(baseline: List[Dict[str, Any]], candidate: List[Dict[str, Any]], *, dimension: str,
            baseline_value: str, candidate_value: str, agent: Optional[str] = None,
            min_runs: int = 20, alpha: float = 0.05) -> Dict[str, Any]:
    """Compare two cohorts of run profiles. Each item: {"trace_id", "profile": {...}}."""
    A = [r["profile"]["features"] for r in baseline]
    B = [r["profile"]["features"] for r in candidate]
    na, nb = len(A), len(B)
    sa = sum(f["outcome"] == "success" for f in A)
    sb = sum(f["outcome"] == "success" for f in B)
    pval = _z_test(sa, na, sb, nb)
    ra = sa / na if na else None
    rb = sb / nb if nb else None

    calls_a, calls_b, err_a, err_b = Counter(), Counter(), Counter(), Counter()
    for f in A:
        calls_a.update(f["tool_calls"])
        err_a.update(f["tool_errors"])
    for f in B:
        calls_b.update(f["tool_calls"])
        err_b.update(f["tool_errors"])
    jsd = _jsd(dict(calls_a), dict(calls_b)) if (calls_a or calls_b) else 0.0
    tools = []
    for t in sorted(set(calls_a) | set(calls_b)):
        per_a = calls_a[t] / na if na else 0
        per_b = calls_b[t] / nb if nb else 0
        er_a = err_a[t] / calls_a[t] if calls_a[t] else None
        er_b = err_b[t] / calls_b[t] if calls_b[t] else None
        tools.append({
            "tool": t, "calls_per_run_a": round(per_a, 3), "calls_per_run_b": round(per_b, 3),
            "error_rate_a": None if er_a is None else round(er_a, 3),
            "error_rate_b": None if er_b is None else round(er_b, 3),
            "status": "new" if not calls_a[t] else "gone" if not calls_b[t] else "changed"
            if abs(per_b - per_a) >= max(0.25, 0.5 * per_a) else "same",
        })

    def metric(key):
        ma, mb = _mean([f[key] for f in A]), _mean([f[key] for f in B])
        return {"a": None if ma is None else round(ma, 2), "b": None if mb is None else round(mb, 2),
                "change": _change(ma, mb)}

    rc_a = Counter(f["root_cause"] for f in A if f["outcome"] == "failure" and f["root_cause"])
    rc_b = Counter(f["root_cause"] for f in B if f["outcome"] == "failure" and f["root_cause"])

    enough = na >= min_runs and nb >= min_runs
    signals = []
    if pval is not None and pval < alpha and ra is not None and abs(rb - ra) >= 0.05:
        signals.append(f"success rate {'fell' if rb < ra else 'rose'} from {ra:.0%} to {rb:.0%} (p={pval:.3g})")
    if jsd >= 0.1:
        moved = [t for t in tools if t["status"] != "same"]
        detail = ", ".join(f"{t['tool']} {t['status']}" for t in moved[:4])
        signals.append(f"tool-call mix shifted (JSD {jsd:.2f}{': ' + detail if detail else ''})")
    for t in tools:
        if t["error_rate_b"] is not None and (t["error_rate_a"] or 0) + 0.1 <= t["error_rate_b"] and calls_b[t["tool"]] >= 5:
            signals.append(f"{t['tool']} error rate rose from {(t['error_rate_a'] or 0):.0%} to {t['error_rate_b']:.0%}")
    steps, tokens, latency = metric("steps"), metric("tokens"), metric("duration_ms")
    for name, m in (("steps per run", steps), ("tokens per run", tokens)):
        if m["change"] is not None and abs(m["change"]) >= 0.25:
            signals.append(f"{name} {'up' if m['change'] > 0 else 'down'} {abs(m['change']):.0%}")
    for cat in set(rc_b) - set(rc_a):
        if rc_b[cat] >= 3:
            signals.append(f"new failure cause: {cat} in {rc_b[cat]} runs")

    regress = ra is not None and rb is not None and rb < ra and pval is not None and pval < alpha
    if not enough:
        verdict, severity = "insufficient_data", "none"
    elif not signals:
        verdict, severity = "no_drift", "none"
    else:
        verdict = "drift"
        severity = "high" if regress and (ra - rb) >= 0.1 else "medium" if regress or jsd >= 0.2 else "low"

    headline = (f"{agent or 'Agent'}: {dimension} {baseline_value} → {candidate_value}: " +
                ("; ".join(signals) if signals else "no significant change" if enough else
                 f"needs {min_runs} runs per cohort (have {na} and {nb})"))
    return {
        "agent": agent, "dimension": dimension, "baseline": baseline_value, "candidate": candidate_value,
        "verdict": verdict, "severity": severity, "headline": headline, "signals": signals,
        "runs": {"a": na, "b": nb},
        "success_rate": {"a": None if ra is None else round(ra, 4), "b": None if rb is None else round(rb, 4),
                         "p_value": None if pval is None else round(pval, 5)},
        "tool_mix": {"jsd": jsd, "tools": tools},
        "steps": steps, "tokens": tokens, "duration_ms": latency,
        "root_causes": {"a": dict(rc_a), "b": dict(rc_b)},
        "examples": {"a": [r["trace_id"] for r in baseline[-3:]], "b": [r["trace_id"] for r in candidate[-3:]]},
    }


# ----------------------------------------------------------------------------- store-backed operations

def cohort(store, agent: str, dimension: str, value: str, project: Optional[str] = None) -> List[Dict[str, Any]]:
    if dimension not in DIMENSIONS:
        raise ValueError(f"dimension must be one of {DIMENSIONS}")
    return [r for r in store.run_profiles(agent=agent, project=project)
            if r["profile"]["dims"].get(dimension) == value]


def values(store, agent: str, dimension: str, project: Optional[str] = None) -> List[Dict[str, Any]]:
    """Distinct values of a dimension in first-seen order, with run counts."""
    seen: Dict[str, Dict[str, Any]] = {}
    for r in store.run_profiles(agent=agent, project=project):
        v = r["profile"]["dims"].get(dimension)
        if v is None:
            continue
        e = seen.setdefault(v, {"value": v, "runs": 0, "first_seen": r["started_at"]})
        e["runs"] += 1
        e["last_seen"] = r["started_at"]
    return list(seen.values())


def save_report(store, report: Dict[str, Any], project: Optional[str] = None) -> Dict[str, Any]:
    store.execute(
        "INSERT OR REPLACE INTO drift_reports(project, agent, dimension, baseline, candidate, verdict, severity, "
        "created_at, report) VALUES (?,?,?,?,?,?,?,?,?)",
        (project, report["agent"], report["dimension"], report["baseline"], report["candidate"],
         report["verdict"], report["severity"], time.time_ns(), json.dumps(report, default=str)))
    return report


def list_reports(store, agent: Optional[str] = None, verdict: Optional[str] = None) -> List[Dict[str, Any]]:
    q, args = "SELECT * FROM drift_reports WHERE 1=1", []
    if agent:
        q += " AND agent=?"
        args.append(agent)
    if verdict:
        q += " AND verdict=?"
        args.append(verdict)
    rows = store.query(q + " ORDER BY created_at DESC", args)
    for r in rows:
        r["report"] = json.loads(r["report"])
    return rows


def scan(store, agent: str, project: Optional[str] = None, min_runs: int = 20) -> List[Dict[str, Any]]:
    """Compare the newest value of each dimension against the one before it.

    Writes a report once per (agent, dimension, baseline, candidate) pair when both
    cohorts have ``min_runs`` runs, and rewrites it as more runs arrive.
    """
    written = []
    for dim in DIMENSIONS:
        vals = values(store, agent, dim, project)
        if len(vals) < 2:
            continue
        prev, new = vals[-2], vals[-1]
        if prev["runs"] < min_runs or new["runs"] < min_runs:
            continue
        existing = store.query("SELECT report FROM drift_reports WHERE project IS ? AND agent=? AND dimension=? "
                               "AND baseline=? AND candidate=?", (project, agent, dim, prev["value"], new["value"]))
        if existing and json.loads(existing[0]["report"])["runs"]["b"] == new["runs"]:
            continue
        rep = compare(cohort(store, agent, dim, prev["value"], project), cohort(store, agent, dim, new["value"], project),
                      dimension=dim, baseline_value=prev["value"], candidate_value=new["value"], agent=agent,
                      min_runs=min_runs)
        rep["previous_verdict"] = json.loads(existing[0]["report"])["verdict"] if existing else None
        written.append(save_report(store, rep, project))
    return written
