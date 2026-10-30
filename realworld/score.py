"""Score root-cause systems on real τ-bench failures, per LABELING.md.

    python realworld/score.py --split dev            # look at misses
    python realworld/score.py --split test --json r.json
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

AGREE = {  # fixed in LABELING.md
    "called_wrong_tool": {"wrong_tool", "planning_failure", "unconfirmed_action", "policy_violation"},
    "used_wrong_tool_argument": {"bad_llm_output", "invalid_tool_args", "ungrounded_argument"},
    "goal_partially_completed": {"incomplete_task", "planning_failure"},
    "missing_output": {"hallucination", "incomplete_task"},
    # live SQL harness only (LABELING.md, "Live harnesses"): a wrong final answer
    "wrong_answer": {"bad_llm_output", "hallucination", "invalid_tool_args", "ungrounded_argument", "judged"},
}


def tracedown(bundle, judge=None):
    from tracedown_server.rca import analyze
    rep = analyze(bundle["spans"], bundle["blobs"], judge=judge)
    return [{"category": f["category"], "step": f["step"]} for f in rep["findings"]]


class CachedJudge:
    """Wraps a judge so each (case, model) is asked once; answers are kept in data/."""

    def __init__(self, spec: str):
        from tracedown_server.rca.judge import from_spec
        self.fn, self.spec = from_spec(spec), spec
        self.path = os.path.join(HERE, "data", "judge-cache-" + spec.replace(":", "_").replace("/", "_") + ".jsonl")
        self.cache = {}
        if os.path.exists(self.path):
            for line in open(self.path):
                d = json.loads(line)
                self.cache[d["key"]] = d["answer"]
        self.calls = 0

    def for_case(self, case_id: str):
        def call(prompt: str) -> str:
            if case_id not in self.cache:
                self.cache[case_id] = self.fn(prompt)
                self.calls += 1
                with open(self.path, "a") as f:
                    f.write(json.dumps({"key": case_id, "answer": self.cache[case_id]}) + "\n")
            return self.cache[case_id]
        return call


def first_error(bundle):
    errs = sorted((s for s in bundle["spans"] if s["status"]["code"] == "ERROR" and s["kind"] == "tool"),
                  key=lambda s: s["attributes"]["tdn.step"])
    if not errs:
        return [{"category": "reported_failure", "step": 0}]
    s = errs[0]
    return [{"category": "invalid_tool_args" if s["attributes"].get("tdn.tool.args_valid") is False else "tool_failure",
             "step": s["attributes"]["tdn.step"]}]


def last_llm(bundle):
    llm = sorted((s for s in bundle["spans"] if s["kind"] == "llm"), key=lambda s: s["attributes"]["tdn.step"])
    return [{"category": "bad_llm_output", "step": llm[-1]["attributes"]["tdn.step"]}] if llm else []


def _writer(bundle):
    """Live harness bundles name their state-changing tools; τ-bench uses the naming rule."""
    if bundle.get("write_tools") is not None:
        return set(bundle["write_tools"]).__contains__
    sys.path.insert(0, HERE)
    from tau_bench_import import is_write
    return is_write


def last_write(bundle):
    """Baseline that knows the domain: blame the last state-changing tool call."""
    is_write = _writer(bundle)
    w = sorted((s for s in bundle["spans"] if s["kind"] == "tool" and is_write(s["attributes"].get("gen_ai.tool.name", ""))),
               key=lambda s: s["attributes"]["tdn.step"])
    return [{"category": "wrong_tool", "step": w[-1]["attributes"]["tdn.step"]}] if w else []


def first_write(bundle):
    """Strongest simple baseline: blame the first state-changing call (the point of no return)."""
    is_write = _writer(bundle)
    w = sorted((s for s in bundle["spans"] if s["kind"] == "tool" and is_write(s["attributes"].get("gen_ai.tool.name", ""))),
               key=lambda s: s["attributes"]["tdn.step"])
    return [{"category": "wrong_tool", "step": w[0]["attributes"]["tdn.step"]}] if w else []


SYSTEMS: Dict[str, Callable] = {"tracedown": tracedown, "baseline: first tool error": first_error,
                                "baseline: last LLM turn": last_llm, "baseline: last write call": last_write,
                                "baseline: first write call": first_write}


def load(split: str, path: str = None) -> List[Dict[str, Any]]:
    with open(path or os.path.join(HERE, "data", f"{split}.jsonl")) as f:
        cases = [json.loads(line) for line in f]
    return [c for c in cases if not c.get("success", False)]  # live files also hold the passing runs


def located(pred, label) -> bool:
    return pred["step"] is not None and pred["step"] in {label["step"], label.get("emitted_by_step")}


def score(system, cases, judge=None):
    n_loc = loc1 = loc3 = cat1 = detected = 0
    by_cat = defaultdict(lambda: [0, 0, 0])  # n, category agree, localized
    misses, top_cats = [], Counter()
    for c in cases:
        lab = c["label"]
        ranked = (system(c["bundle"], judge.for_case(c["id"])) if judge else system(c["bundle"]))[:3]
        top = ranked[0] if ranked else {"category": None, "step": None}
        top_cats[top["category"]] += 1
        agree = top["category"] in AGREE[lab["category"]]
        cat1 += agree
        detected += top["category"] not in (None, "reported_failure")
        b = by_cat[lab["category"]]
        b[0] += 1
        b[1] += agree
        if lab["step"] is not None:
            n_loc += 1
            l1 = located(top, lab)
            loc1 += l1
            loc3 += any(located(p, lab) for p in ranked)
            b[2] += l1
            if not l1:
                misses.append({"id": c["id"], "label": lab["category"], "step": lab["step"],
                               "got": [f"{p['category']}@{p['step']}" for p in ranked]})
    n = len(cases)
    return {
        "cases": n, "with_step": n_loc,
        "localization@1": round(loc1 / n_loc, 3) if n_loc else None,
        "localization@3": round(loc3 / n_loc, 3) if n_loc else None,
        "category_agreement@1": round(cat1 / n, 3),
        "detection": round(detected / n, 3),
        "per_label": {k: {"n": v[0], "category_agreement": round(v[1] / v[0], 3),
                          "localized": v[2]} for k, v in sorted(by_cat.items())},
        "top_categories": dict(top_cats.most_common()),
        "misses": misses,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=["dev", "test"])
    ap.add_argument("--json")
    ap.add_argument("--misses", type=int, default=0)
    ap.add_argument("--judge", help="e.g. openai:gpt-4.1-mini or anthropic:claude-sonnet-4-5 (needs an API key)")
    ap.add_argument("--limit", type=int, help="score only the first N cases (to cap judge cost)")
    ap.add_argument("--file", help="score a case file from a live harness (realworld/live/*) instead of a split")
    args = ap.parse_args()
    cases = load(args.split, args.file)[: args.limit]
    if not cases:
        print("no failed runs to score")
        return
    res = {name: score(fn, cases) for name, fn in SYSTEMS.items()}
    if args.judge:
        cj = CachedJudge(args.judge)
        res[f"tracedown + judge ({args.judge})"] = score(tracedown, cases, cj)
        print(f"judge calls made: {cj.calls} (cached answers reused: {len(cj.cache) - cj.calls})")
    print(f"real failures · {args.file or 'split=' + args.split} · {len(cases)} runs "
          f"({res['tracedown']['with_step']} with a single cause step)\n")
    print(f"{'system':<28}{'loc@1':>8}{'loc@3':>8}{'category':>10}{'detected':>10}")
    for name, r in res.items():
        pct = lambda v: f"{v:>8.0%}" if v is not None else f"{'n/a':>8}"
        print(f"{name:<28}{pct(r['localization@1'])}{pct(r['localization@3'])}"
              f"{r['category_agreement@1']:>10.0%}{r['detection']:>10.0%}")
    t = res["tracedown"]
    print("\ntracedown by reference label:")
    for k, v in t["per_label"].items():
        print(f"  {k:<26} n={v['n']:<4} category agree {v['category_agreement']:.0%}  localized {v['localized']}")
    print("tracedown top categories:", t["top_categories"])
    for m in t["misses"][:args.misses]:
        print("  miss", m)
    if args.json:
        json.dump({k: {kk: vv for kk, vv in v.items() if kk != "misses"} for k, v in res.items()},
                  open(args.json, "w"), indent=2)


if __name__ == "__main__":
    main()
