"""Score a root-cause system on TDN-RCA-Bench.

A system is a function ``bundle -> ranked list of {"category", "step"}``. Metrics:

* **category@1**: the top candidate has the right category;
* **exact@1**: right category *and* right step (the step that actually caused it);
* **exact@3**: the right (category, step) is among the top three;
* per-category precision, recall and F1 of the top-1 category.

    python benchmark/score.py --split test                 # Tracedown engine + baselines
    python benchmark/score.py --split test --json out.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Callable, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))

System = Callable[[Dict[str, Any]], List[Dict[str, Any]]]


def tdn_engine(bundle: Dict[str, Any]) -> List[Dict[str, Any]]:
    from tracedown_server.rca import analyze
    rep = analyze(bundle["spans"], bundle["blobs"])
    return [{"category": f["category"], "step": f["step"]} for f in rep["findings"]]  # engine's own ranking


def first_error(bundle: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Baseline: blame the first span with ERROR status (what a plain trace viewer highlights)."""
    spans = sorted(bundle["spans"], key=lambda s: s["attributes"].get("tdn.step", 0))
    errs = [s for s in spans if s["status"]["code"] == "ERROR" and s["parent_span_id"]]
    if not errs:
        return [{"category": "reported_failure", "step": 0}]
    s = errs[0]
    cat = "invalid_tool_args" if s["attributes"].get("tdn.tool.args_valid") is False else "tool_failure"
    return [{"category": cat, "step": s["attributes"]["tdn.step"]}]


def last_llm(bundle: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Baseline: blame the last model call ("the LLM got it wrong")."""
    llm = sorted((s for s in bundle["spans"] if s["kind"] == "llm"), key=lambda s: s["attributes"]["tdn.step"])
    return [{"category": "bad_llm_output", "step": llm[-1]["attributes"]["tdn.step"]}] if llm else []


SYSTEMS: Dict[str, System] = {"agent-tracedown": tdn_engine, "baseline: first error": first_error,
                              "baseline: last LLM call": last_llm}


def load(split: str) -> List[Dict[str, Any]]:
    with open(os.path.join(HERE, "data", f"{split}.jsonl")) as f:
        return [json.loads(line) for line in f]


def score(system: System, cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    c1 = e1 = e3 = 0
    pred_counts, gold_counts, tp = Counter(), Counter(), Counter()
    misses = []
    by_distractor = defaultdict(lambda: [0, 0])
    for case in cases:
        gold = case["label"]
        ranked = system(case["bundle"])[:3]
        top = ranked[0] if ranked else {"category": None, "step": None}
        ok_cat = top["category"] == gold["category"]
        ok_exact = ok_cat and top["step"] == gold["step"]
        c1 += ok_cat
        e1 += ok_exact
        e3 += any(p["category"] == gold["category"] and p["step"] == gold["step"] for p in ranked)
        gold_counts[gold["category"]] += 1
        pred_counts[top["category"]] += 1
        tp[gold["category"]] += ok_cat
        by_distractor[case["distractor"]][0] += ok_exact
        by_distractor[case["distractor"]][1] += 1
        if not ok_exact:
            misses.append({"id": case["id"], "gold": gold, "predicted": ranked})
    n = len(cases)
    per_cat = {}
    for cat in sorted(gold_counts):
        p = tp[cat] / pred_counts[cat] if pred_counts[cat] else 0.0
        r = tp[cat] / gold_counts[cat]
        per_cat[cat] = {"precision": round(p, 3), "recall": round(r, 3),
                        "f1": round(2 * p * r / (p + r), 3) if p + r else 0.0, "n": gold_counts[cat]}
    return {
        "cases": n, "category@1": round(c1 / n, 3), "exact@1": round(e1 / n, 3), "exact@3": round(e3 / n, 3),
        "exact@1_with_distractor": round(by_distractor[True][0] / max(by_distractor[True][1], 1), 3),
        "exact@1_without_distractor": round(by_distractor[False][0] / max(by_distractor[False][1], 1), 3),
        "per_category": per_cat, "misses": misses,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="holdout", choices=["dev", "test", "holdout"])
    ap.add_argument("--json")
    args = ap.parse_args()
    cases = load(args.split)
    results = {name: score(fn, cases) for name, fn in SYSTEMS.items()}
    print(f"TDN-RCA-Bench v0 · split={args.split} · {len(cases)} cases\n")
    print(f"{'system':<26}{'category@1':>11}{'exact@1':>9}{'exact@3':>9}{'w/ distractor':>15}")
    for name, r in results.items():
        print(f"{name:<26}{r['category@1']:>11.0%}{r['exact@1']:>9.0%}{r['exact@3']:>9.0%}"
              f"{r['exact@1_with_distractor']:>15.0%}")
    print("\nagent-tracedown per category (top-1 category):")
    for cat, m in results["agent-tracedown"]["per_category"].items():
        print(f"  {cat:<20} P {m['precision']:.2f}  R {m['recall']:.2f}  F1 {m['f1']:.2f}  (n={m['n']})")
    for m in results["agent-tracedown"]["misses"][:10]:
        print(f"  miss {m['id']}: gold {m['gold']['category']}@{m['gold']['step']}  got "
              + ", ".join(f"{p['category']}@{p['step']}" for p in m["predicted"]))
    if args.json:
        with open(args.json, "w") as f:
            json.dump({k: {kk: vv for kk, vv in v.items() if kk != "misses"} for k, v in results.items()}, f, indent=2)


if __name__ == "__main__":
    main()
