"""OTLP/JSON conversion in both directions (OpenTelemetry trace protocol).

TDN spans map onto OTel spans one to one: ``gen_ai.*`` attributes pass through and
the TDN kind and payload digests ride along as ``tdn.*`` attributes.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List


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
        "scopeSpans": [{"scope": {"name": "agent-tracedown", "version": "0.4.2"}, "spans": otlp_spans}],
    }]}


def otlp_to_abx(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Map OTLP/JSON spans with gen_ai.* attributes onto TDN span kinds."""
    op_kind = {"invoke_agent": "agent", "create_agent": "agent", "chat": "llm", "text_completion": "llm",
               "generate_content": "llm", "execute_tool": "tool", "embeddings": "embedding",
               "retrieval": "retrieval"}
    status = {0: "UNSET", 1: "OK", 2: "ERROR"}
    out = []
    for rs in body.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for s in ss.get("spans", []):
                attrs = {}
                for kv in s.get("attributes", []):
                    v = kv.get("value", {})
                    attrs[kv["key"]] = next(iter(v.values()), None) if v else None
                kind = attrs.pop("tdn.kind", None) or op_kind.get(attrs.get("gen_ai.operation.name"), "agent")
                out.append({
                    "trace_id": s["traceId"], "span_id": s["spanId"],
                    "parent_span_id": s.get("parentSpanId") or None, "name": s.get("name", kind),
                    "kind": kind, "start_time": int(s.get("startTimeUnixNano", 0)),
                    "end_time": int(s.get("endTimeUnixNano", 0)) or None,
                    "status": {"code": status.get(s.get("status", {}).get("code", 0), "UNSET"),
                               "message": s.get("status", {}).get("message", "")},
                    "attributes": attrs,
                    "input_digest": attrs.pop("tdn.input.digest", None),
                    "output_digest": attrs.pop("tdn.output.digest", None),
                })
    return out
