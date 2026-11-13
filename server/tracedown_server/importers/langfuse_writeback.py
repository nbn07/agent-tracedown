"""Write a diagnosis back to Langfuse: a ``tracedown.root_cause`` score and a comment.

Used by ``tracedown-import langfuse --write-back`` (or the ``TDN_LANGFUSE_WRITEBACK``
setting). The score is CATEGORICAL, its value the root-cause category, so Langfuse can
group and chart failures by cause; the comment carries the cause, the step it points
at, and the suggested fix for that category.

Posting is best-effort and deduplicated: a trace that already has a
``tracedown.root_cause`` score is left alone, runs without a root cause are skipped,
and a Langfuse failure is reported by the caller as a warning — the diagnosis itself
is produced locally and never depends on Langfuse being reachable.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .langfuse import LangfuseClient

SCORE_NAME = "tracedown.root_cause"


def comment_text(report: Dict[str, Any]) -> str:
    """The trace comment for a diagnosis report: cause, step, suggested fix."""
    rc = report["root_cause"]
    lines = [f"Tracedown diagnosis: {rc['label']} (root cause at step {rc['step']})", "", rc["title"]]
    try:
        from ..incidents import suggest_fix
        fix = suggest_fix(rc["category"], None, rc)
    except Exception:  # noqa: BLE001 - a missing suggestion must not block the write-back
        fix = None
    if fix:
        lines += ["", f"Suggested fix: {fix}"]
    evidence = [e for e in rc.get("evidence") or [] if isinstance(e, str)]
    if evidence:
        lines += ["", "Evidence:"] + [f"- {e}" for e in evidence[:3]]
    return "\n".join(lines)


def post_diagnosis(client: LangfuseClient, trace_id: str, report: Dict[str, Any]) -> str:
    """Post the score and comment for one run. Returns "posted", "already posted" or
    "no root cause". Raises whatever the client raises (LangfuseError / URLError) —
    callers treat that as a warning."""
    if not report.get("root_cause"):
        return "no root cause"
    if client.scores(trace_id, SCORE_NAME):
        return "already posted"
    rc = report["root_cause"]
    client.post("/api/public/scores", {"traceId": trace_id, "name": SCORE_NAME, "value": rc["category"],
                                       "dataType": "CATEGORICAL",
                                       "comment": f"{rc['label']} at step {rc['step']}: {rc['title']}"})
    client.post("/api/public/comments", {"objectType": "TRACE", "objectId": trace_id,
                                         "content": comment_text(report)})
    return "posted"


def write_back(client: LangfuseClient, diagnosed: List[Tuple[str, Dict[str, Any]]]) -> Tuple[int, List[str]]:
    """Post every (trace_id, report) pair. Returns (posted count, warning strings);
    one trace's failure does not stop the others."""
    posted, warnings = 0, []
    for trace_id, report in diagnosed:
        try:
            if post_diagnosis(client, trace_id, report) == "posted":
                posted += 1
        except Exception as e:  # noqa: BLE001 - write-back must never lose the import
            warnings.append(f"trace {trace_id}: {e}")
    return posted, warnings
