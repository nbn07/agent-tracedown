"""Root-cause pipeline: detect -> link -> walk back -> rank -> render.

The engine looks for the *earliest step whose bad output flowed into the
failure*. A tool that rejects "Friday" as a date is the symptom; the LLM step
that produced "Friday" is the cause.
"""
from __future__ import annotations

import json
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Set

from .detectors import DETECTORS, Finding, RunView

FINAL = "__final_answer__"
_TRIVIAL = {"true", "false", "null", "none", "text", "user", "assistant", "system"}


def _leaves(value: Any, out: Optional[List[str]] = None, structured: bool = False) -> List[str]:
    out = [] if out is None else out
    if isinstance(value, str):
        v = value.strip()
        if v[:1] in "{[":  # model output that is itself JSON: look inside it too
            try:
                _leaves(json.loads(v), out, structured=True)
            except ValueError:
                pass
        # Short values ("Q3", "EU") only count when they are a whole structured field.
        if (len(v) >= 3 or (structured and len(v) >= 2)) and v.lower() not in _TRIVIAL:
            out.append(v)
    elif isinstance(value, dict):
        for k, v in value.items():
            if k not in ("usage", "finish_reason", "id", "score", "version", "stale"):
                _leaves(v, out, structured=True)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _leaves(v, out, structured=structured)
    elif isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value) >= 100:
        out.append(str(value))
    return out


def build_dataflow(run: RunView) -> Dict[str, Set[str]]:
    """Return ``upstream[span_id] = {span ids whose output appears in this span's input}``.

    The root's final answer is the pseudo-node FINAL.
    """
    upstream: Dict[str, Set[str]] = defaultdict(set)
    producers = [s for s in run.spans if s["kind"] in ("llm", "tool", "retrieval", "memory") and s.get("output_digest")]
    consumers = [(s["span_id"], run.step(s), json.dumps(run.input(s), default=str))
                 for s in run.spans if s.get("input_digest") and s["parent_span_id"]]
    final_text = run.output(run.root)
    if final_text is not None:
        consumers.append((FINAL, 10 ** 9, json.dumps(final_text, default=str)))
    for p in producers:
        leaves = _leaves(run.output(p))
        if not leaves:
            continue
        pstep = run.step(p)
        for cid, cstep, ctext in consumers:
            if cstep <= pstep or cid == p["span_id"]:
                continue
            if any((json.dumps(leaf) if len(leaf) < 4 else json.dumps(leaf)[1:-1]) in ctext for leaf in leaves):
                upstream[cid].add(p["span_id"])
    return upstream


def _ancestors(start: str, upstream: Dict[str, Set[str]]) -> List[str]:
    seen, order, q = set(), [], deque([start])
    while q:
        cur = q.popleft()
        for up in upstream.get(cur, ()):
            if up not in seen:
                seen.add(up)
                order.append(up)
                q.append(up)
    return order


def analyze(spans: List[Dict[str, Any]], blobs: Dict[str, str]) -> Dict[str, Any]:
    if not spans:
        raise ValueError("run has no spans")
    run = RunView(spans, blobs)
    findings: List[Finding] = []
    for det in DETECTORS:
        findings.extend(det(run))
    for i, f in enumerate(findings):
        f.id = f"f{i + 1}"

    upstream = build_dataflow(run)
    root_causes: Set[str] = set()
    by_span: Dict[str, List[Finding]] = defaultdict(list)
    for f in findings:
        by_span[f.span_id].append(f)

    for sym in [f for f in findings if f.symptom]:
        start = FINAL if sym.span_id == run.root["span_id"] else sym.span_id
        anc = _ancestors(start, upstream)
        same = [g for g in by_span.get(sym.span_id, []) if not g.symptom and g.category == "wrong_tool"]
        flagged = same or [g for sid in anc for g in by_span.get(sid, []) if not g.symptom]
        if flagged:
            rc = min(flagged, key=lambda g: (g.step, -g.confidence))
            sym.caused_by = rc.id
            target = "the final answer" if start == FINAL else f"step {sym.step}"
            if not same:
                rc.evidence.append(f"its output flows into {target}, where the failure surfaced")
            root_causes.add(rc.id)
            continue
        # No flagged ancestor: blame the nearest upstream LLM step whose value the failing step used.
        llm_up = [run.by_id[s] for s in anc if s in run.by_id and run.by_id[s]["kind"] == "llm"]
        if llm_up and sym.category in ("invalid_tool_args", "tool_failure"):
            src = max(llm_up, key=run.step)
            sym_span = run.by_id[sym.span_id]
            used = [leaf for leaf in _leaves(run.output(src))
                    if json.dumps(leaf)[1:-1] in json.dumps(run.input(sym_span), default=str)]
            offending = next((u for u in used if u in sym_span["status"]["message"]), used[0] if used else None)
            strong = offending is not None and offending in sym_span["status"]["message"]
            if sym.category == "tool_failure" and not strong:
                # Valid arguments and an error that does not mention them: the tool or the service
                # behind it failed (a 500, a timeout). The model is not to blame.
                root_causes.add(sym.id)
                continue
            agent = src["attributes"].get("tdn.agent.name", "agent")
            tool = sym_span["attributes"].get("gen_ai.tool.name", sym_span["name"])
            derived = Finding(
                category="bad_llm_output",
                title=f"{agent} produced {offending!r} at step {run.step(src)}, which {tool}() could not use",
                span_id=src["span_id"], step=run.step(src), confidence=0.88 if strong else 0.7,
                detector="dataflow_walkback",
                evidence=[f"step {run.step(src)} output {offending!r} is used verbatim in step {sym.step} arguments",
                          f"step {sym.step} error names the same value" if strong else
                          "value reached the failing step through data flow"],
            )
            derived.id = f"f{len(findings) + 1}"
            findings.append(derived)
            sym.caused_by = derived.id
            root_causes.add(derived.id)

    # A content producer (retrieval, memory read) whose output no later step used cannot have
    # caused the failure, however bad it looks: rank it below connected findings.
    consumed = {up for ups in upstream.values() for up in ups}
    unused = {f.id for f in findings if not f.symptom and f.id not in root_causes
              and run.by_id.get(f.span_id, {}).get("kind") in ("retrieval", "memory")
              and f.span_id not in consumed and f.category != "memory_corruption"
              and run.by_id[f.span_id]["attributes"].get("tdn.retrieval.count", 1) != 0}  # empty = the problem
    for f in findings:
        if f.id in unused:
            f.evidence.append("its output was not used by any later step")

    def score(f: Finding) -> float:
        s = f.confidence * (1.2 if f.id in root_causes else 1.0)
        if f.id in unused:
            s *= 0.6
        if f.symptom and f.caused_by:
            s *= 0.8  # the symptom ranks below what caused it
        return min(s, 0.99)

    ranked = sorted(findings, key=lambda f: (-score(f), f.step))
    out = [dict(f.as_dict(), score=round(score(f), 3), root_cause=f.id in root_causes) for f in ranked]
    report = {
        "trace_id": run.root["trace_id"],
        "agent": run.root["attributes"].get("gen_ai.agent.name", run.root["name"]),
        "outcome": run.root["attributes"].get("tdn.run.outcome",
                                              "failure" if run.root["status"]["code"] == "ERROR" else "success"),
        "findings": out,
        "root_cause": out[0] if out else None,
        "dataflow": {k: sorted(v) for k, v in upstream.items()},
    }
    report["stack_trace"] = render_stack_trace(report, run)
    return report


def render_stack_trace(report: Dict[str, Any], run: RunView) -> str:
    """Fixed, pasteable text format (Slack, GitHub issues, terminals)."""
    from datetime import datetime, timezone
    top = report["root_cause"]
    started = datetime.fromtimestamp(run.root["start_time"] / 1e9, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    head = f"  run      {report['trace_id'][:8]}…  {report['agent']}  {started}"
    if top is None:
        return f"No failure detected ({report['outcome']}).\n{head}"
    symptom = next((f for f in report["findings"] if f["symptom"] and f["caused_by"] == top["id"]), None)
    lead = symptom or top

    def frame(f, verb):
        s = run.by_id.get(f["span_id"], run.root)
        agent = s["attributes"].get("tdn.agent.name") or s["attributes"].get("gen_ai.agent.name", "run")
        line = f"  {verb:<10}step {f['step']}  {agent} → {s['name']}"
        detail = s["status"]["message"] if s["status"]["code"] == "ERROR" and verb == "at" else f["title"]
        return f"{line}\n              {detail}"

    lines = [f"AgentFailureException: {lead['label']}", head, frame(lead, "at")]
    if symptom is not None:
        lines.append(frame(top, "caused by"))
    lines.append(f"  root cause: {top['label']} (confidence {top['confidence']:.2f})")
    for ev in top["evidence"][:3]:
        lines.append(f"  evidence:   {ev}")
    others = [f for f in report["findings"] if f["id"] not in (top["id"], lead["id"])]
    if others:
        lines.append("  also:       " + "; ".join(f"{f['label']} (step {f['step']}, {f['confidence']:.2f})" for f in others[:3]))
    lines.append(f"  next:       tracedown replay {report['trace_id'][:8]} --from-step {top['step']}")
    return "\n".join(lines)
