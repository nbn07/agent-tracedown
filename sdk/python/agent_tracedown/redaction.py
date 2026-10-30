"""Redaction hooks: scrub secrets and PII before payloads leave the process.

    tdn.init(project="x", endpoint=..., redact=tdn.redaction.default_redactor)
"""
from __future__ import annotations

import re
from typing import Any

PATTERNS = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    (re.compile(r"\b(?:sk|pk|rk|tdn)[-_][A-Za-z0-9_-]{16,}\b"), "[API_KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[AWS_KEY]"),
    (re.compile(r"\b(?:\d[ -]?){13,16}\b"), "[CARD]"),
    (re.compile(r"\+?\d{1,2}[ .-]?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}\b"), "[PHONE]"),
]


def redact_text(text: str) -> str:
    for pattern, repl in PATTERNS:
        text = pattern.sub(repl, text)
    return text


def default_redactor(value: Any) -> Any:
    """Recursively redact strings inside dicts and lists."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: default_redactor(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [default_redactor(v) for v in value]
    return value
