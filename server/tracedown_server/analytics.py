"""Agent performance analytics and Prometheus metrics.

Computed from run profiles (written by the pipeline when a run finishes) and the
span table:

* success and failure rate, latency p50/p95, steps per run;
* tokens and cost per task (and per *successful* task);
* hallucination rate and response quality (share of answer claims that sources support);
* tool reliability: calls, error rate, latency per tool;
* retrieval effectiveness: empty and low-score rates, stale-chunk rate, and how often a
  retrieval actually reached the final answer;
* user feedback score when the SDK reports one (``run.feedback(score)``);
* daily time series.

Cost: see ``pricing``. A reported ``tdn.llm.cost_usd`` wins; otherwise tokens × the
built-in price table for current Anthropic, OpenAI and Google models, overridden by
``TDN_PRICES`` and ``PUT /api/v1/settings/prices``. Runs that used a model with no known
price report their cost as unknown rather than zero.
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

from . import pricing

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""


def user_prices(store) -> Dict[str, Dict[str, Any]]:
    """Prices saved through the API (they override the built-in table and ``TDN_PRICES``)."""
    try:
        rows = store.query("SELECT value FROM settings WHERE key='prices'")
    except Exception:  # noqa: BLE001 - a bare store without the settings table
        return {}
    return json.loads(rows[0]["value"]) if rows else {}


def get_prices(store) -> Dict[str, Dict[str, Any]]:
    """Effective price table: built-in, then ``TDN_PRICES``, then saved prices."""
    return pricing.merged(user_prices(store))


def set_prices(store, prices: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Save price overrides (merged over the built-in table; an empty object clears them)."""
    pricing.validate(prices)
    store.execute("INSERT INTO settings(key, value) VALUES ('prices', ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(prices),))
    return prices


def _price_for(prices: Dict[str, Dict[str, Any]], model: Optional[str]) -> Optional[Dict[str, Any]]:
    return pricing.price_for(prices, model)


def run_cost(profile: Dict[str, Any], prices: Dict[str, Dict[str, Any]],
             at_ns: Optional[int] = None) -> Optional[float]:
    """Cost of a run in USD, or None when any model call in it has no known price."""
    c = pricing.run_cost_from_profile(profile, prices, at_ns)
    return c["cost_usd"] if c["cost_complete"] else None


def _pct(xs: List[float], q: float) -> Optional[float]:
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 2)


def overview(store, project: Optional[str] = None, agent: Optional[str] = None,
             days: int = 7) -> Dict[str, Any]:
    since = time.time_ns() - days * 86400 * 10 ** 9
    runs = store.run_profiles(agent=agent, project=project, since_ns=since)
    prices = get_prices(store)

    by_agent: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in runs:
        by_agent[r["agent_name"]].append(r)

    def summarize(rs: List[Dict[str, Any]]) -> Dict[str, Any]:
        f = [r["profile"]["features"] for r in rs]
        n = len(f)
        ok = sum(x["outcome"] == "success" for x in f)
        costs = [run_cost(r["profile"], prices, r["started_at"]) for r in rs]
        known = [c for c in costs if c is not None]
        answered = [x["claims"] for x in f if x.get("claims") and x["claims"]["claims"] > 0
                    and x["claims"]["evidence_units"] > 1]
        halluc = sum(1 for c in answered if c["unsupported"] + c["contradicted"] > 0)
        quality = [c["supported"] / c["claims"] for c in answered]
        fb = [x["feedback"] for x in f if isinstance(x.get("feedback"), (int, float))]
        return {
            "runs": n, "successes": ok, "failures": n - ok,
            "success_rate": round(ok / n, 4) if n else None,
            "failure_rate": round(1 - ok / n, 4) if n else None,
            "latency_ms": {"p50": _pct([x["duration_ms"] for x in f], 0.5), "p95": _pct([x["duration_ms"] for x in f], 0.95)},
            "steps_per_run": round(sum(x["steps"] for x in f) / n, 2) if n else None,
            "tokens": {"total": sum(x["tokens"] for x in f), "per_run": round(sum(x["tokens"] for x in f) / n, 1) if n else None},
            "cost_usd": {
                "total": round(sum(known), 6) if known else None,
                "per_task": round(sum(known) / len(known), 6) if known else None,
                "per_successful_task": round(sum(known) / ok, 6) if known and ok and len(known) == n else None,
                "runs_without_price": n - len(known),
            },
            "hallucination_rate": round(halluc / len(answered), 4) if answered else None,
            "response_quality": round(sum(quality) / len(quality), 4) if quality else None,
            "feedback_score": round(sum(fb) / len(fb), 3) if fb else None,
            "top_root_causes": Counter(x["root_cause"] for x in f if x.get("root_cause")).most_common(5),
        }

    # Retrieval effectiveness, per retriever source.
    retr: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"queries": 0, "empty": 0, "low_score": 0,
                                                          "stale_hits": 0, "used": 0, "used_known": 0, "scores": []})
    for r in runs:
        for x in r["profile"]["features"].get("retrieval", []):
            e = retr[x["source"]]
            e["queries"] += 1
            e["empty"] += x["chunks"] == 0
            if isinstance(x.get("max_score"), (int, float)):
                e["scores"].append(x["max_score"])
                e["low_score"] += x["max_score"] < 0.5
            e["stale_hits"] += x["stale"] > 0
            if x.get("used") is not None:
                e["used_known"] += 1
                e["used"] += bool(x["used"])
    retrieval = [{
        "source": src, "queries": e["queries"],
        "empty_rate": round(e["empty"] / e["queries"], 4), "low_score_rate": round(e["low_score"] / e["queries"], 4),
        "stale_rate": round(e["stale_hits"] / e["queries"], 4),
        "avg_max_score": round(sum(e["scores"]) / len(e["scores"]), 3) if e["scores"] else None,
        "reached_answer_rate": round(e["used"] / e["used_known"], 4) if e["used_known"] else None,
    } for src, e in sorted(retr.items())]

    daily: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"runs": 0, "failures": 0, "cost_usd": 0.0,
                                                           "cost_by_agent": defaultdict(float)})
    for r in runs:
        day = time.strftime("%Y-%m-%d", time.gmtime(r["started_at"] / 1e9))
        d = daily[day]
        d["runs"] += 1
        d["failures"] += r["profile"]["features"]["outcome"] == "failure"
        # priced steps count even when another step in the run has no price
        c = pricing.run_cost_from_profile(r["profile"], prices, r["started_at"])["cost_usd"] or 0
        d["cost_usd"] = round(d["cost_usd"] + c, 6)
        d["cost_by_agent"][r["agent_name"]] += c
    for d in daily.values():
        d["cost_by_agent"] = {a: round(v, 6) for a, v in sorted(d["cost_by_agent"].items())}

    return {
        "window_days": days, "project": project, "agent": agent,
        "overall": summarize(runs),
        "agents": {a: summarize(rs) for a, rs in sorted(by_agent.items())},
        "tools": store.tool_stats(project, since),
        "retrieval": retrieval,
        "daily": [{"day": k, **v} for k, v in sorted(daily.items())],
        "models": pricing.by_model(runs, prices),
        "prices_configured": sorted(prices),
    }


# ----------------------------------------------------------------------------- Prometheus

def _esc(v: Any) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def prometheus(store, project: Optional[str] = None) -> str:
    """Prometheus text exposition (cumulative over retained data)."""
    runs = store.run_profiles(project=project)
    prices = get_prices(store)
    lines: List[str] = []

    def metric(name, kind, help_, samples):
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            lab = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
            lines.append(f"{name}{{{lab}}} {value}" if lab else f"{name} {value}")

    c_runs, c_tok, c_cost, c_hall, c_rc = Counter(), Counter(), Counter(), Counter(), Counter()
    for r in runs:
        f = r["profile"]["features"]
        c_runs[(r["project"], r["agent_name"], f["outcome"])] += 1
        for call in f.get("llm", []):
            c_tok[(call.get("model") or "unknown", "input")] += call["input_tokens"]
            c_tok[(call.get("model") or "unknown", "output")] += call["output_tokens"]
            usd, _ = pricing.call_cost(call, prices, call.get("at_ns") or r.get("started_at"))
            if usd is not None:
                c_cost[call.get("model") or "unknown"] += usd
        cl = f.get("claims")
        if cl and cl["unsupported"] + cl["contradicted"] > 0:
            c_hall[(r["project"], r["agent_name"])] += 1
        for cat in f.get("categories", []):
            c_rc[(r["agent_name"], cat)] += 1
    metric("tdn_runs_total", "counter", "Agent runs by outcome.",
           [({"project": p, "agent": a, "outcome": o}, n) for (p, a, o), n in sorted(c_runs.items())])
    metric("tdn_llm_tokens_total", "counter", "LLM tokens by model and direction.",
           [({"model": m, "direction": d}, n) for (m, d), n in sorted(c_tok.items())])
    metric("tdn_llm_cost_usd_total", "counter", "LLM cost in USD (priced models only).",
           [({"model": m}, round(v, 6)) for m, v in sorted(c_cost.items())])
    metric("tdn_hallucination_runs_total", "counter", "Runs with at least one unsupported or contradicted claim.",
           [({"project": p, "agent": a}, n) for (p, a), n in sorted(c_hall.items())])
    metric("tdn_rca_findings_total", "counter", "Runs with a finding of each root-cause category.",
           [({"agent": a, "category": c}, n) for (a, c), n in sorted(c_rc.items())])
    tools = store.tool_stats(project, None)
    metric("tdn_tool_calls_total", "counter", "Tool calls.", [({"tool": t["tool"]}, t["calls"]) for t in tools])
    metric("tdn_tool_errors_total", "counter", "Tool calls that failed.", [({"tool": t["tool"]}, t["errors"]) for t in tools])
    try:
        open_inc = store.query("SELECT severity, COUNT(*) n FROM incidents WHERE status != 'resolved' GROUP BY severity")
    except Exception:  # noqa: BLE001 - incidents table may not exist on a bare store
        open_inc = []
    metric("tdn_incidents_open", "gauge", "Open incidents by severity.",
           [({"severity": r["severity"]}, r["n"]) for r in open_inc])
    return "\n".join(lines) + "\n"
