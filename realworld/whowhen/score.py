"""Score Tracedown (rules only) on Who&When: which agent, and which step, caused the failure.

    python realworld/whowhen/convert.py --repo ../Agents_Failure_Attribution
    python realworld/whowhen/score.py [--json realworld/whowhen/results.json]

Accuracy is exact: predicted agent == ``mistake_agent``; predicted step (Who&When history
index) == ``mistake_step``. The official ``evaluate.py`` counts a step as right when the
label is a *substring* of the prediction ("1" matches "12"); we report the strict version.
Spans carry ``tdn.ww.index`` / ``tdn.ww.agent``. A finding on the run itself (no step) is
skipped and the next finding used; with none left, the prediction is the last step.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path[:0] = [os.path.join(ROOT, "server"), HERE]
from convert import TERMINALS, is_failure_output  # noqa: E402

# Table 1 of Zhang et al. (ICML 2025), GPT-4o judge; "with ground truth" setting first.
PAPER = {
    ("algorithm", True): {"random": (29.10, 19.06), "all-at-once": (54.33, 12.50), "step-by-step": (35.20, 25.51), "binary search": (44.13, 23.98)},
    ("hand", True): {"random": (12.00, 4.16), "all-at-once": (55.17, 5.26), "step-by-step": (34.48, 7.02), "binary search": (51.72, 6.90)},
    ("algorithm", False): {"random": (29.10, 19.06), "all-at-once": (51.12, 13.53), "step-by-step": (26.02, 15.31), "binary search": (30.11, 16.59)},
    ("hand", False): {"random": (12.00, 4.16), "all-at-once": (53.44, 3.51), "step-by-step": (32.75, 8.77), "binary search": (36.21, 6.90)},
}

Pred = List[Tuple[str, int]]  # ranked (agent, step)


import threading

JUDGE = {"fn": None, "cache": {}, "path": None, "key": "", "lock": threading.Lock()}


def _judge_for(case):
    """A judge callable that answers from the cache, asking the model only once per log."""
    if JUDGE["fn"] is None:
        return None
    key = f"{JUDGE['key']}:{case.get('id') or case.get('case_id') or id(case)}"

    def call(prompt: str) -> str:
        if "who caused the failure" in prompt:  # fault verdict: not scored here
            return "{}"
        if key not in JUDGE["cache"]:
            for attempt in range(5):
                try:
                    ans = JUDGE["fn"](prompt)
                    break
                except Exception as e:  # noqa: BLE001 - rate limits: wait and retry
                    if "429" not in str(e) and "529" not in str(e) and "overloaded" not in str(e).lower():
                        raise
                    import time
                    time.sleep(5 * (attempt + 1))
            else:
                ans = JUDGE["fn"](prompt)
            with JUDGE["lock"]:
                JUDGE["cache"][key] = ans
                with open(JUDGE["path"], "a") as f:
                    f.write(json.dumps({"key": key, "answer": ans}) + "\n")
        return JUDGE["cache"][key]
    return call


def tracedown(case) -> Pred:
    return _tracedown(case, None)


def tracedown_judged(case) -> Pred:
    return _tracedown(case, _judge_for(case))


def _tracedown(case, judge) -> Pred:
    from tracedown_server.rca import analyze
    b = case["bundle"]
    by_id = {s["span_id"]: s for s in b["spans"]}
    rep = analyze(b["spans"], b["blobs"], judge=judge)
    out: Pred = []
    for f in rep["findings"]:
        s = by_id.get(f["span_id"])
        if s is None or "tdn.ww.index" not in s["attributes"]:
            continue
        idx, agent = s["attributes"]["tdn.ww.index"], s["attributes"]["tdn.ww.agent"]
        if agent in TERMINALS:  # code output: blame the message (and agent) that wrote the code
            idx = _speaker_index_before(case, idx)
            agent = case["roles"][idx]
        if (agent, idx) not in out:
            out.append((agent, idx))
    return out or last_step(case)


def _speaker_before(case, idx: int) -> str:
    for j in range(idx - 1, -1, -1):
        r = case["roles"][j]
        if r not in TERMINALS and r != "human":
            return r
    return case["roles"][idx]


def _speaker_index_before(case, idx: int) -> int:
    for j in range(idx - 1, -1, -1):
        if case["roles"][j] not in TERMINALS and case["roles"][j] != "human":
            return j
    return idx


def _content(case, idx: int) -> str:
    for s in case["bundle"]["spans"]:
        if s["attributes"].get("tdn.ww.index") == idx and s["kind"] == "tool":
            return s["status"]["message"] or ""
    return ""


def first_error(case) -> Pred:
    for i, r in enumerate(case["roles"]):
        if _content(case, i):
            if r in TERMINALS:
                i = _speaker_index_before(case, i)
            return [(case["roles"][i], i)]
    return last_step(case)


def last_step(case) -> Pred:
    i = case["n_steps"] - 1
    r = case["roles"][i]
    return [(_speaker_before(case, i) if r in TERMINALS or r == "human" else r, i)]


def first_agent_turn(case) -> Pred:
    for i, r in enumerate(case["roles"]):
        if r not in TERMINALS and r != "human":
            return [(r, i)]
    return last_step(case)


SYSTEMS = {"Tracedown (rules only)": tracedown, "baseline: first failure-looking output": first_error,
           "baseline: last step": last_step, "baseline: first agent turn": first_agent_turn}


def score(cases, system) -> Dict[str, float]:
    a = s1 = s3 = both = 0
    for c in cases:
        p = system(c)
        a += p[0][0] == c["mistake_agent"]
        s1 += p[0][1] == c["mistake_step"]
        both += p[0][0] == c["mistake_agent"] and p[0][1] == c["mistake_step"]
        s3 += c["mistake_step"] in [x[1] for x in p[:3]]
    n = len(cases)
    # "agent+step": both right at once, the strict measure some papers report (e.g. AgentDebugX)
    return {"agent": 100 * a / n, "step": 100 * s1 / n, "step@3": 100 * s3 / n, "agent+step": 100 * both / n}


def random_expected(cases) -> Dict[str, float]:
    n = len(cases)
    return {"agent": 100 * sum(1 / max(1, len(c["agents"])) for c in cases) / n,
            "step": 100 * sum(1 / c["n_steps"] for c in cases) / n, "step@3": 100 * sum(min(1, 3 / c["n_steps"]) for c in cases) / n}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    ap.add_argument("--judge", help="add the LLM judge, e.g. anthropic:claude-haiku-5-5 (answers are cached)")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    if a.judge:
        from tracedown_server.rca.judge import from_spec
        JUDGE["fn"] = from_spec(a.judge)
        JUDGE["path"] = os.path.join(ROOT, "realworld", "data", "judge-cache-whowhen-" + a.judge.replace(":", "_") + ".jsonl")
        shipped = os.path.join(HERE, "judge-answers-" + a.judge.split(":", 1)[-1].replace("claude-", "") + ".jsonl")
        for path in (shipped, JUDGE["path"]):  # answers saved in the repo, then local ones
            if os.path.exists(path):
                for line in open(path):
                    d = json.loads(line)
                    JUDGE["cache"][d["key"]] = d["answer"]
        SYSTEMS[f"Tracedown + judge ({a.judge})"] = tracedown_judged
    results = {}
    for subset, name in (("algorithm", "Algorithm-Generated"), ("hand", "Hand-Crafted")):
        for gt in (True, False):
            path = os.path.join(ROOT, "realworld", "data", f"whowhen-{subset}{'-gt' if gt else ''}.jsonl")
            cases = [json.loads(line) for line in open(path)]
            if a.judge:  # fill the cache in parallel, then score sequentially
                from concurrent.futures import ThreadPoolExecutor
                JUDGE["key"] = f"{subset}-{'gt' if gt else 'nogt'}"
                for i, c in enumerate(cases):
                    c.setdefault("case_id", c.get("id", i))
                with ThreadPoolExecutor(a.workers) as ex:
                    list(ex.map(lambda c: tracedown_judged(c), cases))
            res = {k: score(cases, fn) for k, fn in SYSTEMS.items()}
            res["baseline: random (expected)"] = random_expected(cases)
            key = f"{name} · {'with' if gt else 'without'} ground truth"
            results[key] = {"n": len(cases), "ours": res, "paper_gpt4o": PAPER[(subset, gt)]}
            print(f"\n{key} · {len(cases)} logs")
            print(f"  {'system':<40}{'agent':>8}{'step':>8}{'step@3':>8}")
            for k, v in res.items():
                print(f"  {k:<40}{v['agent']:>7.1f}%{v['step']:>7.1f}%{v['step@3']:>7.1f}%")
            for k, (ag, st) in PAPER[(subset, gt)].items():
                print(f"  {'paper, GPT-4o: ' + k:<40}{ag:>7.1f}%{st:>7.1f}%{'—':>8}")
    if a.json:
        json.dump(results, open(a.json, "w"), indent=2)


if __name__ == "__main__":
    main()
