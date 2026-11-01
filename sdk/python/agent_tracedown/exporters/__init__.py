"""Exporters ship finished runs (spans + content blobs) somewhere.

All exporters are stdlib-only so the SDK adds no dependencies to an agent.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


class Exporter:
    def export(self, spans: List[Dict[str, Any]], blobs: Dict[str, str]) -> None:  # pragma: no cover
        raise NotImplementedError


class MemoryExporter(Exporter):
    """Keeps runs in memory. Used by tests and by replay."""

    def __init__(self) -> None:
        self.spans: List[Dict[str, Any]] = []
        self.blobs: Dict[str, str] = {}

    def export(self, spans, blobs):
        self.spans.extend(spans)
        self.blobs.update(blobs)

    def trace(self, trace_id: str) -> List[Dict[str, Any]]:
        return [s for s in self.spans if s["trace_id"] == trace_id]

    def clear(self) -> None:
        self.spans.clear()
        self.blobs.clear()


class ConsoleExporter(Exporter):
    """Prints a compact tree of each run to stderr."""

    def __init__(self, stream=None) -> None:
        self.stream = stream or sys.stderr

    def export(self, spans, blobs):
        children: Dict[Optional[str], list] = {}
        for s in spans:
            children.setdefault(s["parent_span_id"], []).append(s)

        def walk(parent, depth):
            for s in sorted(children.get(parent, []), key=lambda x: x["start_time"]):
                ms = ((s["end_time"] or s["start_time"]) - s["start_time"]) / 1e6
                mark = "x" if s["status"]["code"] == "ERROR" else "-"
                step = s["attributes"].get("tdn.step", "")
                print(f"{'  ' * depth}{mark} [{step}] {s['kind']:<9} {s['name']}  {ms:.1f}ms", file=self.stream)
                walk(s["span_id"], depth + 1)

        walk(None, 0)


class HttpExporter(Exporter):
    """Posts spans and blobs to an TDN server's ingest API."""

    def __init__(self, endpoint: str, api_key: Optional[str] = None, project: str = "default", timeout: float = 5.0):
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.project = project
        self.timeout = timeout
        self.last_error: Optional[str] = None

    def _post(self, path: str, payload: Dict[str, Any]) -> None:
        req = urllib.request.Request(
            self.endpoint + path,
            data=json.dumps(payload, default=str).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp.read()

    def export(self, spans, blobs):
        # Telemetry must never crash the agent: failures are recorded, not raised.
        try:
            if blobs:
                self._post("/v1/ingest/blobs", {"project": self.project, "blobs": blobs})
            self._post("/v1/ingest/spans", {"project": self.project, "spans": spans})
            self.last_error = None
        except (urllib.error.URLError, OSError) as exc:
            self.last_error = str(exc)
            print(f"[agent-tracedown] export failed: {exc}", file=sys.stderr)


class OtlpHttpExporter(Exporter):
    """Send runs straight to any OTLP/HTTP endpoint: an OpenTelemetry Collector, Jaeger
    (``:4318``), Grafana Tempo or Alloy, or the Datadog Agent's OTLP intake.

        tdn.init(exporter=tdn.OtlpHttpExporter("http://localhost:4318/v1/traces"))

    Payload content is not sent (spans carry digests); pair with ``HttpExporter`` via
    ``FanoutExporter`` to keep replay and RCA in Tracedown.
    """

    def __init__(self, endpoint: str = "http://localhost:4318/v1/traces", headers: Optional[Dict[str, str]] = None,
                 service_name: str = "agent", timeout: float = 5.0) -> None:
        self.endpoint, self.headers, self.service_name, self.timeout = endpoint, headers or {}, service_name, timeout
        self.last_error: Optional[str] = None

    def export(self, spans, blobs):
        req = urllib.request.Request(self.endpoint, method="POST",
                                     data=json.dumps(to_otlp_json(spans, self.service_name)).encode(),
                                     headers={"Content-Type": "application/json", **self.headers})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                r.read()
            self.last_error = None
        except (urllib.error.URLError, OSError) as exc:
            self.last_error = str(exc)
            print(f"[agent-tracedown] OTLP export failed: {exc}", file=sys.stderr)


class FanoutExporter(Exporter):
    """Send every run to several exporters (e.g. TDN server + OTLP collector)."""

    def __init__(self, *exporters: Exporter) -> None:
        self.exporters = exporters

    def export(self, spans, blobs):
        for e in self.exporters:
            e.export(spans, blobs)


def to_otlp_json(spans: List[Dict[str, Any]], service_name: str = "agent") -> Dict[str, Any]:
    """Convert TDN spans to an OTLP/JSON ExportTraceServiceRequest body.

    Lets TDN data go to Jaeger, Tempo, Datadog or any OTLP endpoint unchanged.
    """

    def attr(k, v):
        if isinstance(v, bool):
            val = {"boolValue": v}
        elif isinstance(v, int):
            val = {"intValue": str(v)}
        elif isinstance(v, float):
            val = {"doubleValue": v}
        else:
            val = {"stringValue": v if isinstance(v, str) else json.dumps(v, default=str)}
        return {"key": k, "value": val}

    status_map = {"UNSET": 0, "OK": 1, "ERROR": 2}
    otlp_spans = []
    for s in spans:
        attrs = dict(s["attributes"])
        attrs["tdn.kind"] = s["kind"]
        if s.get("input_digest"):
            attrs["tdn.input.digest"] = s["input_digest"]
        if s.get("output_digest"):
            attrs["tdn.output.digest"] = s["output_digest"]
        otlp_spans.append({
            "traceId": s["trace_id"],
            "spanId": s["span_id"],
            "parentSpanId": s["parent_span_id"] or "",
            "name": s["name"],
            "kind": 1 if s["kind"] in ("agent", "decision", "memory") else 3,  # INTERNAL / CLIENT
            "startTimeUnixNano": str(s["start_time"]),
            "endTimeUnixNano": str(s["end_time"] or s["start_time"]),
            "attributes": [attr(k, v) for k, v in attrs.items()],
            "status": {"code": status_map[s["status"]["code"]], "message": s["status"]["message"]},
        })
    return {"resourceSpans": [{
        "resource": {"attributes": [attr("service.name", service_name)]},
        "scopeSpans": [{"scope": {"name": "agent-tracedown", "version": "0.4.3"}, "spans": otlp_spans}],
    }]}
