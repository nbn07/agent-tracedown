"""Shared plumbing for importers: normalized steps in, Tracedown spans and blobs out.

Every importer (OpenTelemetry, Langfuse, ...) reduces its source to a flat list of
:class:`Node` objects: one per step, with a parent link, a kind, timestamps, input,
output and an error. :func:`build_run` turns one trace's nodes into the span and blob
format the SDK produces, so the rest of Tracedown (root-cause analysis, replay views,
the server) cannot tell an imported run from a natively recorded one.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

OPERATION = {"agent": "invoke_agent", "llm": "chat", "tool": "execute_tool", "retrieval": "retrieval",
             "embedding": "embeddings", "handoff": "invoke_agent"}
KINDS = {"agent", "llm", "tool", "retrieval", "embedding", "memory", "decision", "handoff"}


@dataclass
class Node:
    id: str
    parent: Optional[str]
    name: str
    kind: Optional[str]                 # None = generic wrapper (chain, span, workflow step)
    start: int                          # unix nanoseconds
    end: Optional[int] = None
    input: Any = None
    output: Any = None
    error: Optional[str] = None
    attrs: Dict[str, Any] = field(default_factory=dict)
    agent: Optional[str] = None         # agent name, when the source names one
    tool: Optional[str] = None          # tool name, for tool steps
    model: Optional[str] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    step: Optional[int] = None          # keep a step number the source already assigned
    input_digest: Optional[str] = None  # keep digests the source already carries (round trips)
    output_digest: Optional[str] = None


def put_blob(blobs: Dict[str, str], value: Any) -> Optional[str]:
    """Same encoding as the SDK, so digests match natively recorded runs."""
    if value is None:
        return None
    text = json.dumps(value, sort_keys=True, default=str)
    d = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    blobs[d] = text
    return d


def maybe_json(v: Any) -> Any:
    if isinstance(v, str) and v[:1] in "{[\"":
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def message_text(value: Any) -> Optional[str]:
    """The text a model produced, from the shapes providers and SDKs use for it."""
    value = maybe_json(value)
    if value is None or isinstance(value, (int, float, bool)):
        return None if value is None else str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for k in ("text", "content", "completion", "output", "message"):
            if k in value:
                t = message_text(value[k])
                if t:
                    return t
        if "choices" in value:
            return message_text(value["choices"])
        if "parts" in value:
            return message_text(value["parts"])
        calls = value.get("tool_calls")
        if calls:
            return message_text(calls)
        if value.get("type") in ("function", "tool_call") or "function" in value or "arguments" in value:
            fn = value.get("function") or value
            return f"[calls {fn.get('name', 'tool')}({fn.get('arguments', '')})]"
        return None
    if isinstance(value, list):
        parts = [message_text(v) for v in value]
        parts = [p for p in parts if p]
        return "\n".join(parts) or None
    return str(value)


def as_messages(value: Any) -> Any:
    """Normalize a prompt to a list of {role, content} when it looks like one."""
    value = maybe_json(value)
    if isinstance(value, dict) and isinstance(value.get("messages"), list):
        value = value["messages"]
    if isinstance(value, list) and value and all(isinstance(m, dict) and "role" in m for m in value):
        out = []
        for m in value:
            content = m.get("content")
            if not isinstance(content, str):
                content = message_text(content if content is not None else m.get("parts")) or content
            out.append({"role": m["role"], "content": content})
        return out
    return value


def build_run(trace_id: str, nodes: List[Node], run_name: Optional[str] = None, run_input: Any = None,
              run_output: Any = None, failed: Optional[bool] = None, source: str = "import",
              failure_reason: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """One trace's nodes -> (spans, blobs). ``failed``: True/False to state the outcome,
    None to infer it (any error in the run means failure)."""
    by_id = {n.id: n for n in nodes}

    # Generic wrappers (chains, workflow steps) are plumbing: children move up to the
    # nearest real step. A wrapper that failed with no failed child is kept as a step.
    def failed_below(n: Node) -> bool:
        return any(c.error or failed_below(c) for c in nodes if c.parent == n.id)

    keep = {n.id for n in nodes if n.kind is not None or n.parent not in by_id or (n.error and not failed_below(n))}

    def kept_parent(n: Node) -> Optional[str]:
        p = n.parent
        while p is not None and p in by_id and p not in keep:
            p = by_id[p].parent
        return p if p in by_id else None

    kept = [n for n in nodes if n.id in keep]
    for n in kept:
        if n.kind is None:
            n.kind = "agent" if n.parent not in by_id else "tool"
        n.parent = kept_parent(n)

    tops = [n for n in kept if n.parent is None]
    if len(tops) == 1 and tops[0].kind == "agent":
        root = tops[0]
    else:
        start = min((n.start for n in kept), default=0)
        end = max((n.end or n.start for n in kept), default=start)
        root = Node(id=os.urandom(8).hex(), parent=None, name=run_name or "run", kind="agent", start=start, end=end,
                    agent=run_name or "run")
        for n in tops:
            n.parent = root.id
        kept.insert(0, root)
    if run_input is not None and root.input is None:
        root.input = run_input
    if run_output is not None and root.output is None:
        root.output = run_output

    depth: Dict[str, int] = {}

    def d(n: Node) -> int:
        if n.id not in depth:
            depth[n.id] = 0 if n.parent is None else 1 + d(by_id.get(n.parent) or root)
        return depth[n.id]
    by_id = {n.id: n for n in kept}
    order = sorted(kept, key=lambda n: (n is not root, n.start, d(n)))
    have_steps = all(n.step is not None for n in kept)
    if not have_steps:
        for i, n in enumerate(order):
            n.step = i

    def agent_of(n: Optional[Node]) -> Optional[str]:
        while n is not None:
            if n.kind == "agent":
                return n.agent or n.name
            n = by_id.get(n.parent) if n.parent else None
        return None

    blobs: Dict[str, str] = {}
    spans: List[Dict[str, Any]] = []
    any_error = None
    for n in order:
        a: Dict[str, Any] = dict(n.attrs)
        a["tdn.step"] = n.step
        if n.kind in OPERATION:
            a.setdefault("gen_ai.operation.name", OPERATION[n.kind])
        if n.kind == "agent":
            a["gen_ai.agent.name"] = n.agent or n.name.removeprefix("invoke_agent ").strip() or "agent"
        else:
            owner = agent_of(by_id.get(n.parent) if n.parent else None)
            if owner:
                a["tdn.agent.name"] = owner
        if n.kind == "tool":
            a["gen_ai.tool.name"] = n.tool or n.name.removeprefix("execute_tool ").strip()
        if n.model:
            a["gen_ai.request.model"] = n.model
        if n.tokens_in is not None:
            a["gen_ai.usage.input_tokens"] = n.tokens_in
        if n.tokens_out is not None:
            a["gen_ai.usage.output_tokens"] = n.tokens_out
        if n.error and any_error is None:
            any_error = n.error
        inp = as_messages(n.input) if n.kind == "llm" else maybe_json(n.input)
        out = n.output
        if n.kind == "llm" and out is not None and not (isinstance(out, dict) and set(out) == {"text"}):
            out = {"text": message_text(out)}
        elif n.kind != "llm":
            out = maybe_json(out)
        spans.append({
            "trace_id": trace_id, "span_id": n.id, "parent_span_id": n.parent, "name": n.name, "kind": n.kind,
            "start_time": n.start, "end_time": n.end or n.start,
            "status": {"code": "ERROR" if n.error else "OK", "message": n.error or ""},
            "attributes": a,
            "input_digest": n.input_digest or put_blob(blobs, inp),
            "output_digest": n.output_digest or put_blob(blobs, out),
        })
    root_span = spans[0]
    ra = root_span["attributes"]
    if failed is None and ra.get("tdn.run.outcome") in ("success", "failure"):
        failed = ra["tdn.run.outcome"] == "failure"  # the source already said (a Tracedown round trip)
    outcome_failed = failed if failed is not None else (any_error is not None or root.error is not None)
    ra["tdn.run.root"] = True
    ra["tdn.import.source"] = source
    ra["tdn.run.outcome"] = "failure" if outcome_failed else "success"
    if outcome_failed and root_span["status"]["code"] != "ERROR":
        root_span["status"] = {"code": "ERROR", "message": failure_reason or (
            f"imported run marked failed ({any_error})" if any_error else "imported run marked failed")}
    tools = sorted({s["attributes"]["gen_ai.tool.name"] for s in spans if s["kind"] == "tool"})
    if tools and "tdn.tools.available" not in ra:
        ra["tdn.tools.available"] = [{"name": t} for t in tools]
    # the root goes last, as the SDK sends it
    return spans[1:] + spans[:1], blobs
