"""Langfuse importer: diagnose runs you already record in Langfuse.

Two sources:

* the Langfuse public API (cloud or self-hosted), with the usual ``LANGFUSE_PUBLIC_KEY``,
  ``LANGFUSE_SECRET_KEY`` and ``LANGFUSE_HOST`` (or ``LANGFUSE_BASE_URL``) variables;
* a JSON file: a trace with its ``observations`` (``GET /api/public/traces/{id}``), an
  observations page (``{"data": [...]}``), or a plain list of observations.

Observation types map to Tracedown steps: GENERATION -> model call, TOOL -> tool call,
AGENT -> agent, RETRIEVER -> retrieval, EMBEDDING -> embedding, GUARDRAIL -> decision;
SPAN, CHAIN and EVENT are wrappers unless they carry the error. ``level: ERROR`` with its
``statusMessage`` is the step's error.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .common import Node, build_run, maybe_json

_KIND = {"GENERATION": "llm", "AGENT": "agent", "TOOL": "tool", "RETRIEVER": "retrieval", "EMBEDDING": "embedding",
         "GUARDRAIL": "decision", "SPAN": None, "CHAIN": None, "EVENT": None, "EVALUATOR": None}


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


def _usage(o: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    u = o.get("usageDetails") or {}
    old = o.get("usage") or {}
    tin = u.get("input", o.get("inputUsage", old.get("input", old.get("promptTokens"))))
    tout = u.get("output", o.get("outputUsage", old.get("output", old.get("completionTokens"))))
    as_int = lambda v: int(v) if isinstance(v, (int, float)) else None  # noqa: E731
    return as_int(tin), as_int(tout)


def node_from_observation(o: Dict[str, Any]) -> Node:
    t = str(o.get("type") or "SPAN").upper()
    kind = _KIND.get(t)
    name = o.get("name") or t.lower()
    level = str(o.get("level") or "DEFAULT").upper()
    error = (o.get("statusMessage") or f"{t.lower()} {name} failed") if level == "ERROR" else None
    tin, tout = _usage(o)
    meta = o.get("metadata") if isinstance(o.get("metadata"), dict) else {}
    attrs = {"langfuse.observation.id": o.get("id"), "langfuse.observation.type": t}
    if level == "WARNING" and o.get("statusMessage"):
        attrs["langfuse.warning"] = o["statusMessage"]
    if isinstance(meta.get("side_effects"), bool):
        attrs["tdn.tool.side_effects"] = meta["side_effects"]
    start = _ns(o.get("startTime")) or 0
    return Node(id=str(o["id"]), parent=o.get("parentObservationId") or None, name=name, kind=kind, start=start,
                end=_ns(o.get("endTime")) or start, input=maybe_json(o.get("input")), output=maybe_json(o.get("output")),
                error=error, attrs=attrs, agent=name if kind == "agent" else None, tool=name if kind == "tool" else None,
                model=o.get("model"), tokens_in=tin, tokens_out=tout)


def import_observations(observations: List[Dict[str, Any]], trace: Optional[Dict[str, Any]] = None,
                        failed: Optional[bool] = None) -> List[Tuple[str, List[Dict[str, Any]], Dict[str, str]]]:
    """Langfuse observations (and optionally their trace objects) -> [(trace_id, spans, blobs)]."""
    traces = trace if isinstance(trace, dict) and "id" in trace else {}
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for o in observations:
        groups[str(o.get("traceId") or traces.get("id") or "trace")].append(o)
    runs = []
    for trace_id, obs in groups.items():
        meta = traces if traces.get("id") == trace_id else {}
        nodes = [node_from_observation(o) for o in obs if o.get("id")]
        declared = {n.tool: n.attrs.pop("tdn.tool.side_effects") for n in nodes
                    if n.kind == "tool" and "tdn.tool.side_effects" in n.attrs}
        # Langfuse trace ids are not always 32 hex characters; Tracedown keeps them as given
        spans, blobs = build_run(trace_id, nodes, run_name=meta.get("name") or (obs[0].get("traceName") if obs else None),
                                 run_input=maybe_json(meta.get("input")), run_output=maybe_json(meta.get("output")),
                                 failed=failed, source="langfuse")
        root = next(s for s in spans if s["attributes"].get("tdn.run.root"))
        root["attributes"]["langfuse.trace.id"] = trace_id
        if declared:
            root["attributes"]["tdn.tools.available"] = [
                dict(t, **({"side_effects": declared[t["name"]]} if t["name"] in declared else {}))
                for t in root["attributes"].get("tdn.tools.available", [])]
        runs.append((trace_id, spans, blobs))
    return runs


def load_file(path: str) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """A Langfuse JSON export -> (observations, trace object or None)."""
    data = json.load(open(path, encoding="utf-8"))
    if isinstance(data, list):
        return data, None
    if isinstance(data.get("observations"), list):
        obs = data["observations"]
        if obs and isinstance(obs[0], str):
            raise SystemExit(f"{path}: observations are ids only; export the trace with full observations "
                             "or use the API importer")
        return obs, {k: v for k, v in data.items() if k != "observations"}
    if isinstance(data.get("data"), list):
        return data["data"], None
    raise SystemExit(f"{path}: not a Langfuse trace or observation list")


# ----------------------------------------------------------------------------- API client

class LangfuseClient:
    def __init__(self, host: Optional[str] = None, public_key: Optional[str] = None, secret_key: Optional[str] = None,
                 timeout: float = 30):
        self.host = (host or os.environ.get("LANGFUSE_HOST") or os.environ.get("LANGFUSE_BASE_URL")
                     or "https://cloud.langfuse.com").rstrip("/")
        pk = public_key or os.environ.get("LANGFUSE_PUBLIC_KEY")
        sk = secret_key or os.environ.get("LANGFUSE_SECRET_KEY")
        if not pk or not sk:
            raise SystemExit("set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY (or pass --public-key/--secret-key)")
        self.auth = "Basic " + base64.b64encode(f"{pk}:{sk}".encode()).decode()
        self.timeout = timeout

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        q = "?" + urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None}) if params else ""
        req = urllib.request.Request(self.host + path + q, headers={"Authorization": self.auth, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise LangfuseError(e.code, e.read()[:300].decode(errors="replace"), path) from None

    def observations(self, trace_id: Optional[str] = None, since: Optional[datetime] = None,
                     level: Optional[str] = None, limit: int = 1000) -> List[Dict[str, Any]]:
        """Observations API v2 (cursor pages); falls back to v1 (page numbers) on older servers."""
        now = datetime.now(timezone.utc)
        # every v2 request is time-bounded; a lookup by trace id should not miss old traces
        start = since or now - timedelta(days=3650 if trace_id else 30)
        params = {"traceId": trace_id, "level": level, "fromStartTime": start.isoformat(),
                  "toStartTime": now.isoformat(), "fields": "core,basic,io,model,usage,metadata", "limit": min(limit, 1000)}
        out: List[Dict[str, Any]] = []
        try:
            cursor = None
            while True:
                page = self.get("/api/public/v2/observations", dict(params, cursor=cursor))
                out += page.get("data", [])
                cursor = (page.get("meta") or {}).get("cursor")
                if not cursor or len(out) >= limit:
                    return out[:limit]
        except LangfuseError as e:
            if e.status not in (404, 405):
                raise
        page_no, out = 1, []
        while True:
            page = self.get("/api/public/observations", {"traceId": trace_id, "level": level, "page": page_no,
                                                         "limit": 100, "fromStartTime": start.isoformat()})
            out += page.get("data", [])
            meta = page.get("meta") or {}
            if page_no >= int(meta.get("totalPages") or 1) or len(out) >= limit:
                return out[:limit]
            page_no += 1

    def trace(self, trace_id: str) -> Dict[str, Any]:
        try:
            return self.get(f"/api/public/traces/{urllib.parse.quote(trace_id)}")
        except LangfuseError:
            return {"id": trace_id}


class LangfuseError(RuntimeError):
    def __init__(self, status: int, body: str, path: str):
        super().__init__(f"Langfuse {path}: HTTP {status}: {body}")
        self.status = status


def fetch_runs(client: LangfuseClient, trace_ids: Iterable[str] = (), errors_since: Optional[datetime] = None,
               limit: int = 20, failed: Optional[bool] = None):
    """Fetch traces by id, or every trace with an ERROR-level observation since a time."""
    ids = list(trace_ids)
    if not ids and errors_since is not None:
        seen: List[str] = []
        for o in client.observations(since=errors_since, level="ERROR", limit=1000):
            t = o.get("traceId")
            if t and t not in seen:
                seen.append(t)
        ids = seen[:limit]
    runs = []
    for tid in ids:
        trace = client.trace(tid)
        obs = trace.get("observations") if isinstance(trace.get("observations"), list) else None
        if not obs or isinstance(obs[0], str):
            obs = client.observations(trace_id=tid)
        runs += import_observations(obs, trace, failed=failed)
    return runs
