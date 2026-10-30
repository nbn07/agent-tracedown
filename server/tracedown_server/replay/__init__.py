"""Replay planning and run diffing.

The server plans a replay (which steps are served from the recording, which
call live); the SDK executes it, because only the SDK has the agent's code.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

MODES = ("exact", "from-step", "swap", "full")
REPLAYABLE = ("llm", "tool", "retrieval", "memory")


def plan(spans: List[Dict[str, Any]], blobs: Dict[str, str], mode: str,
         from_step: Optional[int] = None, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if mode == "from-step" and from_step is None:
        raise ValueError("from-step mode needs from_step")
    steps, missing = [], []
    for s in sorted(spans, key=lambda x: x["attributes"].get("tdn.step", 0)):
        if s["kind"] not in REPLAYABLE:
            continue
        n = s["attributes"].get("tdn.step", 0)
        if mode == "exact":
            source = "recorded"
        elif mode == "full":
            source = "live"
        elif mode == "from-step":
            source = "recorded" if n < from_step else "live"
        else:  # swap
            source = "live" if s["kind"] == "llm" else "recorded"
        if source == "recorded" and s.get("output_digest") and s["output_digest"] not in blobs:
            missing.append(n)
        steps.append({"step": n, "kind": s["kind"], "name": s["name"], "source": source})
    trace_id = spans[0]["trace_id"] if spans else None
    args = [f"mode={mode!r}"] + ([f"from_step={from_step}"] if from_step is not None else []) + \
           ([f"overrides={overrides!r}"] if overrides else [])
    return {
        "source_trace_id": trace_id, "mode": mode, "from_step": from_step, "overrides": overrides or {},
        "steps": steps,
        "recorded": sum(1 for s in steps if s["source"] == "recorded"),
        "live": sum(1 for s in steps if s["source"] == "live"),
        "replayable": not missing,
        "missing_payload_steps": missing,
        "sdk_snippet": (
            "import agent_tracedown as tdn\n"
            f"bundle = tdn.fetch_bundle(ENDPOINT, {trace_id!r})\n"
            f"with tdn.replay(bundle, {', '.join(args)}):\n"
            "    run_my_agent(...)  # same entry point as the original run"
        ),
    }


def diff(a_spans: List[Dict[str, Any]], b_spans: List[Dict[str, Any]],
         a_blobs: Dict[str, str], b_blobs: Dict[str, str]) -> Dict[str, Any]:
    """Align two runs step by step and report where they diverge."""
    def by_step(spans):
        return {s["attributes"].get("tdn.step", 0): s for s in spans}

    A, B = by_step(a_spans), by_step(b_spans)
    rows, first = [], None
    for n in sorted(set(A) | set(B)):
        a, b = A.get(n), B.get(n)
        if a is None or b is None:
            change = "added" if a is None else "removed"
        elif (a["kind"], a["name"]) != (b["kind"], b["name"]):
            change = "different_step"
        elif a["status"]["code"] != b["status"]["code"]:
            change = "status"
        elif a.get("input_digest") != b.get("input_digest"):
            change = "input"
        elif a.get("output_digest") != b.get("output_digest"):
            change = "output"
        else:
            change = "same"
        leaf = (a or b)["kind"] not in ("agent",)  # container spans only echo their children
        if change != "same" and first is None and leaf:
            first = n
        side = lambda s, blobs: None if s is None else {  # noqa: E731
            "name": s["name"], "kind": s["kind"], "status": s["status"]["code"],
            "output": blobs.get(s.get("output_digest") or "", None)}
        rows.append({"step": n, "change": change, "a": side(a, a_blobs), "b": side(b, b_blobs)})
    root = lambda spans: next((s for s in spans if not s["parent_span_id"]), {})  # noqa: E731
    return {
        "a": root(a_spans).get("trace_id"), "b": root(b_spans).get("trace_id"),
        "outcome_a": root(a_spans).get("attributes", {}).get("tdn.run.outcome"),
        "outcome_b": root(b_spans).get("attributes", {}).get("tdn.run.outcome"),
        "first_divergence": first, "steps": rows,
    }
