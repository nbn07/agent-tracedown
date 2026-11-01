"""Proven fixes: check that Tracedown blamed the right step, and that a fix works.

    results = tdn.try_fixes(trace_id, agent=lambda: calendar_agent(),
                            tools={"calendar_create_event": fake_create_event})
    for r in results:
        print(r["verdict"], r["change"])   # works / does_not_work / blocked / not_tried

    report = tdn.confirm_cause(trace_id, corrected='{"start": "2026-11-06T15:00:00-05:00"}',
                               agent=lambda: calendar_agent(),
                               tools={"calendar_create_event": fake_create_event})
    report["verdict"]   # "cause_confirmed", "not_confirmed" or "blocked"

``confirm_cause`` replays the run from the blamed model step, with that step's output
replaced by the output it should have given. Steps before it come from the recording;
from it on, your agent runs live. If the problem goes away and nothing new breaks, the
blamed step really was the cause.

By default no tool runs for real during these replays: pass a stub for each tool the
agent reaches (``tools=``), or name the tools that are safe to run (``live_tools=``).
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Union

from . import tracer
from .exporters import FanoutExporter, HttpExporter


def _endpoint(endpoint: Optional[str], api_key: Optional[str]):
    if endpoint:
        return endpoint.rstrip("/"), api_key
    ex = tracer._config.exporter
    found = [ex] + list(getattr(ex, "exporters", []) if isinstance(ex, FanoutExporter) else [])
    for e in found:
        if isinstance(e, HttpExporter):
            return e.endpoint, api_key or e.api_key
    raise ValueError("confirm_cause needs a Tracedown server: call tdn.init(endpoint=...) or pass endpoint=")


def _call(base: str, api_key: Optional[str], method: str, path: str) -> Dict[str, Any]:
    req = urllib.request.Request(base + path, method=method, data=b"" if method == "POST" else None)
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _as_llm_fn(corrected: Union[str, Dict[str, Any], Callable[..., Any]]) -> Callable[..., Any]:
    if callable(corrected):
        return corrected
    if isinstance(corrected, str):
        corrected = {"text": corrected}
    return lambda *a, **k: corrected


def confirm_cause(trace_id: str, corrected: Union[str, Dict[str, Any], Callable[..., Any]],
                  agent: Callable[[], Any], *, step: Optional[int] = None,
                  tools: Optional[Dict[str, Callable[..., Any]]] = None,
                  live_tools: Optional[List[str]] = None,
                  test: Optional[Callable[[Any], bool]] = None, tries: int = 3,
                  endpoint: Optional[str] = None, api_key: Optional[str] = None) -> Dict[str, Any]:
    """Replay ``trace_id`` with the blamed model step's output replaced by ``corrected``.

    ``corrected``: the text (or ``{"text", ...}`` dict) the model should have returned, or a
    function to call instead of the model at that step. ``agent``: runs your agent once, the
    same way as in the recorded run. ``step``: the model step to correct (default: the root
    cause). ``test``: your own check on what ``agent`` returns. Every one of ``tries`` replays
    must pass.
    """
    base, key = _endpoint(endpoint, api_key)
    report = _call(base, key, "POST", f"/api/v1/runs/{trace_id}/analyze")
    rc = report.get("root_cause")
    bundle = tracer.fetch_bundle(base, trace_id, key)
    kinds = {s["attributes"].get("tdn.step"): s["kind"] for s in bundle["spans"]}
    if step is None:
        if rc is None:
            return {"verdict": "nothing_to_confirm", "passed": False, "tries": [],
                    "summary": "Tracedown found no root cause in this run."}
        step = rc["step"]
        if kinds.get(step) != "llm":
            return {"verdict": "not_a_model_step", "passed": False, "tries": [], "step": step,
                    "summary": f"The root cause is step {step} ({rc.get('label')}), not a model step. "
                               "Pass step= to correct a model step, or fix it and replay."}
    elif kinds.get(step) != "llm":
        raise ValueError(f"step {step} is not a model step")
    overrides: Dict[str, Any] = {"llm_fn": _as_llm_fn(corrected), "steps": [step],
                                 "tools": dict(tools or {}), "live_tools": list(live_tools or [])}
    results, blocked = _replay_and_check(base, key, trace_id, bundle, step, overrides, agent, test, tries)
    if blocked:
        return {"verdict": "blocked", "passed": False, "tries": results, "step": step, "summary": blocked}
    ok = sum(1 for r in results if r.get("passed"))
    passed = ok == len(results)
    return {
        "verdict": "cause_confirmed" if passed else "not_confirmed", "passed": passed, "step": step,
        "summary": (f"Correcting step {step} fixed the run in {ok} of {len(results)} replays."
                    if passed else f"Correcting step {step} fixed the run in only {ok} of {len(results)} "
                    "replays, so it is not the whole cause."),
        "tries": results,
    }


def _replay_and_check(base, key, trace_id, bundle, step, overrides, agent, test, tries):
    """Replay ``tries`` times and check each replay. Returns (checks, blocked message or None)."""
    results: List[Dict[str, Any]] = []
    for _ in range(max(1, tries)):
        user_passed: Optional[bool] = None
        with tracer.replay(bundle, mode="from-step", from_step=step, overrides=overrides):
            rp = tracer._pending_replay.get()
            try:
                value = agent()
            except tracer.SideEffectBlocked as exc:
                return results, str(exc)
            except Exception:  # noqa: BLE001 - the replay's own analysis reports the error
                value, user_passed = None, (False if test else None)
            else:
                if test is not None:
                    try:
                        user_passed = bool(test(value))
                    except Exception:  # noqa: BLE001 - a crashing test is a failing test
                        user_passed = False
            runs = list(rp.runs)
        if not runs:
            raise RuntimeError("agent() did not start a tdn.run, so nothing was replayed")
        q = {"replay": runs[-1]}
        if user_passed is not None:
            q["user_test"] = str(user_passed).lower()
        results.append(_call(base, key, "GET",
                             f"/api/v1/runs/{trace_id}/fix-check?{urllib.parse.urlencode(q)}"))
    return results, None


def try_fixes(trace_id: str, agent: Callable[[], Any], *,
              tools: Optional[Dict[str, Callable[..., Any]]] = None,
              live_tools: Optional[List[str]] = None,
              test: Optional[Callable[[Any], bool]] = None, tries: int = 3,
              endpoint: Optional[str] = None, api_key: Optional[str] = None) -> List[Dict[str, Any]]:
    """Try each fix Tracedown suggests for ``trace_id`` and check whether it works.

    A suggestion Tracedown can try without touching your code (an instruction added to a
    prompt, an outdated or injected document left out) is replayed ``tries`` times; it
    ``works`` only if every replay passes the fix check. Other suggestions come back as
    ``not_tried``, with what to change. Returns the suggestions, most likely first, each with
    ``verdict``: works, does_not_work, blocked or not_tried.
    """
    base, key = _endpoint(endpoint, api_key)
    suggestions = _call(base, key, "GET", f"/api/v1/runs/{trace_id}/suggestions")["suggestions"]
    bundle = None
    out = []
    for s in suggestions:
        attempt = s.get("try")
        if not attempt:
            out.append({**s, "verdict": "not_tried", "checks": [],
                        "summary": s.get("why_not_tried") or "Tracedown cannot try this one for you."})
            continue
        bundle = bundle or tracer.fetch_bundle(base, trace_id, key)
        overrides = {**attempt["overrides"], "tools": dict(tools or {}), "live_tools": list(live_tools or []),
                     "suggestion": s["key"]}
        results, blocked = _replay_and_check(base, key, trace_id, bundle, attempt["from_step"], overrides,
                                             agent, test, tries)
        if blocked:
            out.append({**s, "verdict": "blocked", "checks": results, "summary": blocked})
            continue
        ok = sum(1 for r in results if r.get("passed"))
        works = ok == len(results)
        reasons = next((r["reasons"] for r in results if not r.get("passed")), results[-1]["reasons"])
        out.append({**s, "verdict": "works" if works else "does_not_work", "checks": results,
                    "summary": f"{'Works' if works else 'Did not work'}: {ok} of {len(results)} replays passed "
                               f"({'; '.join(reasons)})."})
    return out
