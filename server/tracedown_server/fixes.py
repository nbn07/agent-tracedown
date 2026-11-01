"""Proven fixes, part 1: did a replay fix the run?

A replay of a failed run with some change (a corrected model output, a new prompt, a
filtered document) passes only if all of these hold:

1. the blamed problem is gone: no finding of the root cause's category at its step
   (for claim problems, anywhere in the replay);
2. nothing new broke: no new finding of 0.7 or more in a category the original run
   did not have;
3. the run finished: no unhandled error, including a replay divergence;
4. the user's own test passed, when one was given.

The run's own outcome is not used: many agents (the demo's refund bot among them)
mark runs failed from outside, after the fact. See docs/HOW-IT-DECIDES.md.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

CLAIM_CATEGORIES = {"hallucination", "stale_source"}
NEW_PROBLEM_MIN = 0.7
IGNORED = {"reported_failure", "unverified_action"}  # outcome markers and priors, not evidence of a new problem


def _same_problem(rc: Dict[str, Any], f: Dict[str, Any]) -> bool:
    if f["category"] != rc["category"]:
        return False
    return rc["category"] in CLAIM_CATEGORIES or f.get("step") == rc.get("step")


def check(original: Dict[str, Any], replay: Optional[Dict[str, Any]],
          user_passed: Optional[bool] = None) -> Dict[str, Any]:
    """Compare the analysis of a failed run with the analysis of its replay.

    ``original`` and ``replay`` are reports from ``rca.analyze`` (``replay`` may be None
    or have no findings when nothing looked wrong). Returns the verdict and the reasons.
    """
    rc = (original or {}).get("root_cause")
    if rc is None:
        return {"passed": False, "verdict": "nothing_to_fix", "checks": {},
                "reasons": ["the original run has no root cause to fix"]}
    found: List[Dict[str, Any]] = list((replay or {}).get("findings") or [])
    still = [f for f in found if _same_problem(rc, f)]
    before = {f["category"] for f in original.get("findings") or []}
    new = [f for f in found if f["category"] not in before and f["category"] not in IGNORED
           and f.get("confidence", 0) >= NEW_PROBLEM_MIN]
    crashed = [f for f in found if f["category"] == "unhandled_error"]

    checks = {
        "blamed_problem_gone": not still,
        "no_new_problem": not new,
        "finished": not crashed,
        "user_test": user_passed,  # None = no test given
    }
    reasons = []
    reasons.append(f"{rc['label']} at step {rc['step']} is gone" if not still else
                   f"{rc['label']} is still there: {still[0]['title'][:160]}")
    if new:
        reasons.append("new problem: " + "; ".join(f"{f['label']} at step {f['step']}" for f in new[:3]))
    if crashed:
        reasons.append(f"the replay did not finish: {crashed[0]['title'][:160]}")
    if user_passed is not None:
        reasons.append("your test passed" if user_passed else "your test failed")
    passed = checks["blamed_problem_gone"] and checks["no_new_problem"] and checks["finished"] \
        and user_passed is not False
    if passed:
        verdict = "fixed" if user_passed else "passes_tracedown_checks"
    else:
        verdict = "not_fixed"
    return {"passed": passed, "verdict": verdict, "checks": checks, "reasons": reasons,
            "root_cause": {k: rc.get(k) for k in ("category", "label", "step", "span_id", "title")}}


def summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Several replays of the same change (models are not deterministic): proven only if all pass."""
    n, ok = len(results), sum(1 for r in results if r.get("passed"))
    return {"passed": n > 0 and ok == n, "tries": n, "passed_tries": ok,
            "summary": f"{ok} of {n} replays passed"}
