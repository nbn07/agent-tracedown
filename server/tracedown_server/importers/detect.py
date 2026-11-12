"""Recognize a pasted or uploaded export and turn it into Tracedown runs.

Accepts what people have at hand: a Langfuse trace or observation list, a LangSmith
run export (a root run with ``child_runs``, or a flat run list), an OTLP/JSON
export (one object, a list, or one object per line), or a Tracedown bundle
({"spans": [...], "blobs": {...}}, as /api/v1/runs/<id>/bundle returns).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from .langfuse import import_observations
from .otel import import_otlp

Runs = List[Tuple[str, List[Dict[str, Any]], Dict[str, str]]]


class UnknownFormat(ValueError):
    pass


def parse_text(text: str) -> Any:
    text = text.strip()
    if not text:
        raise UnknownFormat("nothing to import")
    try:
        return json.loads(text)
    except ValueError:
        try:
            return [json.loads(line) for line in text.splitlines() if line.strip()]  # JSON lines
        except ValueError as exc:
            raise UnknownFormat(f"not JSON: {exc}") from exc


def _is_otlp(d: Any) -> bool:
    return isinstance(d, dict) and isinstance(d.get("resourceSpans"), list)


def _is_langfuse_obs(d: Any) -> bool:
    return isinstance(d, dict) and "traceId" in d and ("type" in d or "startTime" in d)


def _is_langsmith_run(d: Any) -> bool:
    return isinstance(d, dict) and "run_type" in d and ("start_time" in d or "inputs" in d or "child_runs" in d)


def detect(data: Any) -> str:
    if isinstance(data, dict) and isinstance(data.get("spans"), list) and "trace_id" in (data["spans"] or [{}])[0]:
        return "tracedown"
    if _is_otlp(data) or (isinstance(data, list) and data and all(_is_otlp(x) for x in data)):
        return "otlp"
    if _is_langsmith_run(data) or (isinstance(data, list) and data and all(_is_langsmith_run(x) for x in data)):
        return "langsmith"
    if isinstance(data, dict) and isinstance(data.get("observations"), list):
        return "langfuse"
    if isinstance(data, dict) and isinstance(data.get("data"), list) and all(_is_langfuse_obs(x) for x in data["data"]):
        return "langfuse"
    if isinstance(data, list) and data and all(_is_langfuse_obs(x) for x in data):
        return "langfuse"
    raise UnknownFormat("not a Langfuse export, a LangSmith export, an OTLP/JSON export or a Tracedown bundle")


def import_any(data: Any, fmt: str = "auto") -> Tuple[str, Runs]:
    fmt = detect(data) if fmt == "auto" else fmt
    if fmt == "tracedown":
        spans = data["spans"]
        return fmt, [(spans[0]["trace_id"], spans, dict(data.get("blobs") or {}))]
    if fmt == "otlp":
        return fmt, import_otlp(data if isinstance(data, list) else [data])
    if fmt == "langsmith":
        from .langsmith import import_runs
        return fmt, import_runs(data)
    if fmt == "langfuse":
        if isinstance(data, list):
            obs, trace = data, None
        elif isinstance(data.get("observations"), list):
            obs, trace = data["observations"], {k: v for k, v in data.items() if k != "observations"}
            if obs and isinstance(obs[0], str):
                raise UnknownFormat("the observations are ids only; export the trace with full observations")
        else:
            obs, trace = data["data"], None
        return fmt, import_observations(obs, trace)
    raise UnknownFormat(f"unknown format {fmt!r}")
