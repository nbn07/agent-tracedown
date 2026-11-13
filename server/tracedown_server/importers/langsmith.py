"""LangSmith importer: diagnose runs you already record in LangSmith.

LangSmith users can export a trace as JSON (the run tree from the UI, or the runs
of a trace from the API). Two shapes are accepted:

* one root run object with nested ``child_runs``;
* a flat list of runs linked by ``parent_run_id``.

A run carries ``id``, ``name``, ``run_type``, ``inputs``, ``outputs``, ``error`` and
ISO ``start_time`` / ``end_time``. Run types map to Tracedown steps: ``llm`` -> model
call, ``tool`` -> tool call, ``retriever`` -> retrieval, ``embedding`` -> embedding,
``agent`` -> agent; ``chain``, ``prompt``, ``parser`` and the rest are wrappers unless
they carry the error (see :mod:`tracedown_server.importers.common`). A run's ``error``
string is the step's error. Token usage comes from ``usage_metadata`` or the
``llm_output.token_usage`` of an LLM run's outputs, and the model from
``extra.metadata.ls_model_name``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .common import Node, build_run, maybe_json

_KIND = {"llm": "llm", "tool": "tool", "retriever": "retrieval", "embedding": "embedding",
         "agent": "agent", "chain": None, "prompt": None, "parser": None, "listener": None}


def _ns(ts: Optional[str]) -> Optional[int]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1e9)


def _flatten(run: Dict[str, Any], parent: Optional[str], out: List[Dict[str, Any]]) -> None:
    """Nested ``child_runs`` -> a flat list, each run knowing its parent's id."""
    r = dict(run)
    kids = r.pop("child_runs", None) or []
    r["_parent"] = r.get("parent_run_id") or parent
    out.append(r)
    for k in kids:
        if isinstance(k, dict):
            _flatten(k, str(r.get("id")), out)


def _usage(run: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    um = run.get("usage_metadata") if isinstance(run.get("usage_metadata"), dict) else {}
    outputs = run.get("outputs") if isinstance(run.get("outputs"), dict) else {}
    llm_output = outputs.get("llm_output") if isinstance(outputs.get("llm_output"), dict) else {}
    tu = llm_output.get("token_usage") if isinstance(llm_output.get("token_usage"), dict) else {}
    as_int = lambda v: int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None  # noqa: E731
    return (as_int(um.get("input_tokens", tu.get("prompt_tokens"))),
            as_int(um.get("output_tokens", tu.get("completion_tokens"))))


def _model(run: Dict[str, Any]) -> Optional[str]:
    extra = run.get("extra") if isinstance(run.get("extra"), dict) else {}
    meta = extra.get("metadata") if isinstance(extra.get("metadata"), dict) else {}
    outputs = run.get("outputs") if isinstance(run.get("outputs"), dict) else {}
    llm_output = outputs.get("llm_output") if isinstance(outputs.get("llm_output"), dict) else {}
    return meta.get("ls_model_name") or llm_output.get("model_name") or llm_output.get("model")


def node_from_run(r: Dict[str, Any]) -> Node:
    t = str(r.get("run_type") or "chain").lower()
    kind = _KIND.get(t)
    name = r.get("name") or t
    error = r.get("error") or None
    if error is None and str(r.get("status") or "").lower() == "error":
        error = f"{t} {name} failed"
    tin, tout = _usage(r)
    start = _ns(r.get("start_time")) or 0
    attrs = {"langsmith.run.id": str(r.get("id")), "langsmith.run.type": t}
    if r.get("session_name"):
        attrs["langsmith.session"] = r["session_name"]
    return Node(id=str(r["id"]), parent=r.get("_parent") or None, name=name, kind=kind, start=start,
                end=_ns(r.get("end_time")) or start, input=maybe_json(r.get("inputs")),
                output=maybe_json(r.get("outputs")), error=error, attrs=attrs,
                agent=name if kind == "agent" else None, tool=name if kind == "tool" else None,
                model=_model(r), tokens_in=tin, tokens_out=tout)


def import_runs(data: Any, failed: Optional[bool] = None) -> List[Tuple[str, List[Dict[str, Any]], Dict[str, str]]]:
    """A LangSmith export (root run object or flat run list) -> [(trace_id, spans, blobs)]."""
    flat: List[Dict[str, Any]] = []
    if isinstance(data, dict):
        _flatten(data, None, flat)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                _flatten(item, None, flat)
    flat = [r for r in flat if r.get("id")]
    if not flat:
        raise ValueError("no LangSmith runs in the export (runs need an id and a run_type)")
    by_id = {str(r["id"]): r for r in flat}

    def root_of(r: Dict[str, Any]) -> Dict[str, Any]:
        seen = set()
        while r.get("_parent") in by_id and str(r["_parent"]) not in seen:
            seen.add(str(r["id"]))
            r = by_id[str(r["_parent"])]
        return r

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in flat:
        groups.setdefault(str(root_of(r)["id"]), []).append(r)
    runs = []
    for root_id, group in groups.items():
        root_run = by_id[root_id]
        nodes = [node_from_run(r) for r in group]
        trace_id = str(root_run.get("trace_id") or root_id)
        spans, blobs = build_run(trace_id, nodes, run_name=root_run.get("name"),
                                 failed=failed, source="langsmith")
        root = next(s for s in spans if s["attributes"].get("tdn.run.root"))
        root["attributes"]["langsmith.trace.id"] = trace_id
        runs.append((trace_id, spans, blobs))
    return runs


def load_file(path: str) -> Any:
    """A LangSmith JSON export -> the parsed object (root run dict or run list)."""
    data = json.load(open(path, encoding="utf-8"))
    if isinstance(data, dict) and "run_type" not in data and isinstance(data.get("runs"), list):
        return data["runs"]
    if isinstance(data, (dict, list)):
        return data
    raise SystemExit(f"{path}: not a LangSmith run export")
