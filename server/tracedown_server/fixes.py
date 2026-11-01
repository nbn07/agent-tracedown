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


# ----------------------------------------------------------------------------- part 2: suggested fixes

SKIP = {"unverified_action", "reported_failure", "judged", "unhandled_error", "incomplete_task"}
MAX_SUGGESTIONS = 3
_DATE_HINT = ("date-time", "iso-8601", "iso 8601", "date")
GROUNDING_NOTE = ("Use only facts and numbers that appear in the context and tool results above. "
                  "If the answer is not there, say that you don't know.")


def _format_note(tool: str, errors: List[str]) -> str:
    text = "; ".join(errors)
    if any(h in text.lower() for h in _DATE_HINT):
        return (f"When you fill the arguments for {tool}, give every date and time as a full ISO-8601 value "
                "with a time zone, like YYYY-MM-DDTHH:MM:SS+HH:MM. Turn words such as 'Friday' or "
                "'tomorrow' into a date first.")
    return f"The arguments for {tool} must pass these checks: {text}. Check them before you answer."


def _downstream_tool_errors(view, finding: Dict[str, Any], findings: List[Dict[str, Any]]):
    """The tool whose arguments the blamed model step broke, and the tool's validation errors."""
    for f in findings:
        if f.get("caused_by") == finding["id"]:
            span = view.by_id.get(f["span_id"])
            if span is not None and span["kind"] == "tool":
                a = span["attributes"]
                errors = a.get("tdn.tool.validation_errors") or [span.get("status", {}).get("message") or ""]
                return a.get("gen_ai.tool.name", span["name"]), [e for e in errors if e]
    return None, []


def _injected_sources(view, span) -> List[str]:
    from .rca.detectors import _INJECTION, text_of
    out = view.output(span)
    chunks = out if isinstance(out, list) else []
    return sorted({c.get("source") for c in chunks if isinstance(c, dict) and c.get("source")
                   and _INJECTION.search(text_of(c.get("text", c)))})


def _one(view, f: Dict[str, Any], findings: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    from .incidents import suggest_fix, target_of
    span = view.by_id.get(f["span_id"])
    if span is None:
        return None
    kind, step = span["kind"], f["step"]
    change = suggest_fix(f["category"], target_of(f, span), f)
    attempt: Optional[Dict[str, Any]] = None
    why_not = None
    cat = f["category"]
    if cat in ("bad_llm_output", "wrong_tool") and kind == "llm":
        tool, errors = _downstream_tool_errors(view, f, findings)
        if tool and errors:
            note = _format_note(tool, errors)
            change = f"Add to the prompt at step {step}: \"{note}\" Better still, pass {tool}'s schema as a " \
                     f"function definition (structured output), so the model cannot send a bad value."
            attempt = {"from_step": step, "overrides": {"prompt_note": note, "steps": [step]}}
        else:
            why_not = "Tracedown could not tell which check the model's output failed."
    elif cat == "hallucination" and kind == "llm":
        change = f"Add to the prompt at step {step}: \"{GROUNDING_NOTE}\" " + change
        attempt = {"from_step": step, "overrides": {"prompt_note": GROUNDING_NOTE, "steps": [step]}}
    elif cat == "stale_source" and kind == "retrieval":
        attempt = {"from_step": step, "overrides": {"retrieval_drop": {"stale": True, "steps": [step]}}}
    elif cat == "prompt_injection" and kind == "retrieval":
        sources = _injected_sources(view, span)
        if sources:
            attempt = {"from_step": step,
                       "overrides": {"retrieval_drop": {"sources": sources, "steps": [step]}}}
        else:
            why_not = "The instructions could not be traced to one source."
    else:
        why_not = "This fix needs a change in your agent's code, so Tracedown cannot try it for you. " \
                  "Make the change, then replay the run and check it."
    if cat == "tool_failure" and any("still reports success" in e for e in f.get("evidence", [])):
        change = ("The call failed, but the reply told the user it worked. Check the tool's result before "
                  "writing the reply, and tell the user plainly when an action did not happen. ") + change
    return {
        "key": f"{cat}@{step}", "finding": f["id"], "category": cat, "label": f.get("label", cat), "step": step,
        "title": f["title"], "change": change, "try": attempt, "why_not_tried": why_not,
        "tries_as": _describe(attempt),
    }


def _describe(attempt: Optional[Dict[str, Any]]) -> Optional[str]:
    if not attempt:
        return None
    o = attempt["overrides"]
    if "prompt_note" in o:
        return f"replay from step {attempt['from_step']} with the instruction added to that step's prompt"
    drop = o.get("retrieval_drop", {})
    if drop.get("stale"):
        return f"replay from step {attempt['from_step']} without the documents marked outdated"
    return f"replay from step {attempt['from_step']} without content from {', '.join(drop.get('sources', []))}"


def suggest(report: Dict[str, Any], spans: List[Dict[str, Any]], blobs: Dict[str, str]) -> List[Dict[str, Any]]:
    """Suggested fixes for the root cause and the next suspects, most likely first.

    Each suggestion says what to change. When Tracedown can try it without touching your code
    (an instruction added to a prompt, a document left out), ``try`` holds the replay recipe that
    ``tdn.try_fixes`` runs and checks.
    """
    from .rca import RunView
    if not report.get("root_cause") or not spans:
        return []
    fault = report.get("fault") or {}
    if fault.get("verdict") in ("user", "unfinished"):
        rc = report["root_cause"]
        return [{"id": "fix-1", "key": f"{rc['category']}@{rc['step']}", "finding": rc.get("id"), "category": rc["category"], "label": rc.get("label"),
                 "step": rc["step"], "title": rc["title"], "try": None, "tries_as": None,
                 "change": ("This failure was not the agent's mistake: " + fault.get("reason", "") + ". "
                            "Nothing in the agent needs fixing. If it happens often, help users get it right, "
                            "for example by asking them to check their details.") if fault["verdict"] == "user"
                 else "The run stopped while the agent was waiting for the user. Nothing needs fixing yet.",
                 "why_not_tried": "Not an agent bug."}]
    view = RunView(spans, blobs)
    findings = report.get("findings") or []
    rc_id = report["root_cause"].get("id")
    ranked = [f for f in findings if f.get("id") == rc_id or (not f.get("symptom") and f["category"] not in SKIP)]
    ranked.sort(key=lambda f: (f.get("id") != rc_id, -f.get("score", f.get("confidence", 0))))
    out, seen = [], set()
    for f in ranked:
        key = f["category"]  # one suggestion per kind of problem
        if key in seen:
            continue
        seen.add(key)
        s = _one(view, f, findings)
        if s:
            s["id"] = f"fix-{len(out) + 1}"
            out.append(s)
        if len(out) >= MAX_SUGGESTIONS:
            break
    return out


def with_results(suggestions: List[Dict[str, Any]], replays: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Attach replay results to suggestions. ``replays``: [{trace_id, suggestion, check}], newest first.

    A suggestion ``works`` when its latest tries (up to three) all passed; replays that tested
    no suggestion (your own fixes) are returned separately.
    """
    by_key: Dict[str, List[Dict[str, Any]]] = {}
    own = []
    for r in replays:
        (by_key.setdefault(r["suggestion"], []) if r.get("suggestion") else own).append(r)
    for s in suggestions:
        tried = by_key.get(s["key"], [])[:3]
        s["replays"] = tried
        if not s.get("try"):
            s["status"] = "not_tried"
        elif not tried:
            s["status"] = "untested"
        else:
            ok = sum(1 for r in tried if r["check"].get("passed"))
            s["status"] = "works" if ok == len(tried) else "does_not_work"
            s["result"] = f"{ok} of {len(tried)} replays passed"
    return {"suggestions": suggestions, "other_replays": own}
