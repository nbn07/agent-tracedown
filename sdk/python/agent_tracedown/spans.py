"""Span model for the TDN telemetry spec v0.1.

A span is one step of an agent run. Field names follow OpenTelemetry; the
``gen_ai.*`` attributes follow the OTel GenAI semantic conventions and the
``tdn.*`` attributes add what debugging and replay need.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# TDN span kind -> gen_ai.operation.name (None = TDN extension, no OTel equivalent yet)
KIND_TO_OPERATION = {
    "agent": "invoke_agent",
    "llm": "chat",
    "tool": "execute_tool",
    "retrieval": "retrieval",
    "embedding": "embeddings",
    "memory": None,
    "decision": None,
    "handoff": "invoke_agent",
}
SPAN_KINDS = tuple(KIND_TO_OPERATION)


def new_trace_id() -> str:
    return os.urandom(16).hex()


def new_span_id() -> str:
    return os.urandom(8).hex()


def now_ns() -> int:
    return time.time_ns()


def canonical(value: Any) -> str:
    """Stable serialization used for digests and blob storage."""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


@dataclass
class Span:
    trace_id: str
    name: str
    kind: str
    span_id: str = field(default_factory=new_span_id)
    parent_span_id: Optional[str] = None
    start_time: int = field(default_factory=now_ns)
    end_time: Optional[int] = None
    status_code: str = "UNSET"  # UNSET | OK | ERROR
    status_message: str = ""
    attributes: Dict[str, Any] = field(default_factory=dict)
    input_digest: Optional[str] = None
    output_digest: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind not in SPAN_KINDS:
            raise ValueError(f"unknown span kind {self.kind!r}; expected one of {SPAN_KINDS}")
        op = KIND_TO_OPERATION[self.kind]
        if op:
            self.attributes.setdefault("gen_ai.operation.name", op)

    def set(self, key: str, value: Any) -> "Span":
        self.attributes[key] = value
        return self

    def error(self, message: str) -> None:
        self.status_code = "ERROR"
        self.status_message = message

    def end(self) -> None:
        if self.end_time is None:
            self.end_time = now_ns()
        if self.status_code == "UNSET":
            self.status_code = "OK"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "kind": self.kind,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "status": {"code": self.status_code, "message": self.status_message},
            "attributes": self.attributes,
            "input_digest": self.input_digest,
            "output_digest": self.output_digest,
        }
