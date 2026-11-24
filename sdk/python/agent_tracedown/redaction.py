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
    # JWT: three base64url segments, the first starting with "eyJ".
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "[JWT]"),
    # IBAN: format only, no mod-97 checksum. Runs before CARD so the spaced
    # digit groups of an IBAN are not eaten by the card pattern first.
    (re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b"), "[IBAN]"),
    # IPv4: any dotted quad whose octets are all in 0-255 counts as an IP and
    # is redacted, even one that could be a version number (1.2.3.4) - the two
    # are syntactically identical and over-redacting is the safe direction.
    # A quad with an out-of-range octet (300.1.2.3) is left alone.
    (re.compile(r"\b(?:(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.){3}(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\b"), "[IP]"),
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
