"""Server-side PII policies, applied to payloads at ingest.

The SDK can redact before data leaves the agent; this is the second line of defence,
set centrally per project by an admin, so one forgotten SDK setting does not leak
customer data into the observability store.

Policy (per project, JSON):

    {"mode": "redact",                      # off | redact | hash | drop
     "detectors": ["email", "phone", "credit_card", "us_ssn", "ip_address", "api_key", "jwt", "iban"],
     "custom": [{"name": "employee_id", "pattern": "EMP-\\\\d{6}"}],
     "allow_fields": ["model", "tool"]}     # JSON keys never scanned

* ``redact``: replace matches with ``[EMAIL]``-style tokens (replay still works on shape).
* ``hash``: replace with a stable ``[EMAIL:1a2b3c4d]`` so equal values stay comparable.
* ``drop``: keep no payload content at all (digests remain, replay is disabled).

Blobs keep their original digest as the key; the stored content is the redacted form.
Counts per detector are kept in ``pii_events`` for the audit view.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS pii_policies (
    project     TEXT PRIMARY KEY,
    policy      TEXT NOT NULL,
    updated_at  INTEGER NOT NULL,
    updated_by  TEXT
);
CREATE TABLE IF NOT EXISTS pii_events (
    project   TEXT NOT NULL,
    day       TEXT NOT NULL,
    detector  TEXT NOT NULL,
    count     INTEGER NOT NULL,
    PRIMARY KEY (project, day, detector)
);
"""


def _luhn(digits: str) -> bool:
    d = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(d) <= 19:
        return False
    total = 0
    for i, n in enumerate(reversed(d)):
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


DETECTORS: Dict[str, Tuple[re.Pattern, Optional[Any]]] = {
    "email": (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), None),
    "credit_card": (re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), _luhn),
    "us_ssn": (re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"), None),
    # Not inside a longer digit group (a card number that failed the Luhn check stays intact).
    "phone": (re.compile(r"(?<![\w.])(?<!\d[ -])\+?\d{1,3}[\s.-]?\(?\d{2,4}\)?[\s.-]?\d{3,4}[\s.-]?\d{3,4}\b(?![ -]\d)"),
              None),
    "ip_address": (re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"), None),
    "api_key": (re.compile(r"\b(?:sk|pk|rk|tdn|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{16,}\b|\bAKIA[0-9A-Z]{16}\b"), None),
    "jwt": (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), None),
    "iban": (re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,3})?\b"), None),
}
# Order matters: specific patterns first so a card number is not reported as a phone.
ORDER = ["jwt", "api_key", "email", "credit_card", "us_ssn", "iban", "ip_address", "phone"]
DEFAULT_POLICY = {"mode": "off", "detectors": ORDER, "custom": [], "allow_fields": []}


def validate(policy: Dict[str, Any]) -> Dict[str, Any]:
    p = {**DEFAULT_POLICY, **policy}
    if p["mode"] not in ("off", "redact", "hash", "drop"):
        raise ValueError("mode must be off, redact, hash or drop")
    unknown = [d for d in p["detectors"] if d not in DETECTORS]
    if unknown:
        raise ValueError(f"unknown detectors {unknown}; available: {ORDER}")
    for c in p["custom"]:
        if not c.get("name") or not c.get("pattern"):
            raise ValueError("custom detectors need name and pattern")
        re.compile(c["pattern"])
    return p


class Scrubber:
    def __init__(self, policy: Dict[str, Any]) -> None:
        self.policy = validate(policy)
        self.rules: List[Tuple[str, re.Pattern, Any]] = [
            (name, *DETECTORS[name]) for name in ORDER if name in self.policy["detectors"]]
        self.rules += [(c["name"], re.compile(c["pattern"]), None) for c in self.policy["custom"]]
        self.allow = set(self.policy["allow_fields"])
        self.counts: Counter = Counter()

    def _token(self, name: str, value: str) -> str:
        if self.policy["mode"] == "hash":
            return f"[{name.upper()}:{hashlib.sha256(value.encode()).hexdigest()[:8]}]"
        return f"[{name.upper()}]"

    def text(self, s: str) -> str:
        for name, pattern, check in self.rules:
            def sub(m, name=name, check=check):
                if check and not check(m.group(0)):
                    return m.group(0)
                self.counts[name] += 1
                return self._token(name, m.group(0))
            s = pattern.sub(sub, s)
        return s

    def value(self, v: Any, key: Optional[str] = None) -> Any:
        if key in self.allow:
            return v
        if isinstance(v, str):
            return self.text(v)
        if isinstance(v, dict):
            return {k: self.value(x, k) for k, x in v.items()}
        if isinstance(v, list):
            return [self.value(x) for x in v]
        return v

    def blob(self, content: str) -> str:
        if self.policy["mode"] == "drop":
            self.counts["dropped"] += 1
            return json.dumps({"_abx_dropped": True})
        try:
            return json.dumps(self.value(json.loads(content)), sort_keys=True)
        except ValueError:
            return self.text(content)


class PolicyEngine:
    def __init__(self, store) -> None:
        self.store = store
        store.ensure_schema(SCHEMA)

    def get(self, project: str) -> Dict[str, Any]:
        rows = self.store.query("SELECT policy FROM pii_policies WHERE project=?", (project,))
        return json.loads(rows[0]["policy"]) if rows else dict(DEFAULT_POLICY)

    def set(self, project: str, policy: Dict[str, Any], actor: str = "system") -> Dict[str, Any]:
        p = validate(policy)
        self.store.execute("INSERT OR REPLACE INTO pii_policies VALUES (?,?,?,?)",
                           (project, json.dumps(p), time.time_ns(), actor))
        return p

    def __call__(self, project: str, blobs: Dict[str, str]) -> Dict[str, str]:
        policy = self.get(project)
        if policy["mode"] == "off":
            return blobs
        sc = Scrubber(policy)
        out = {d: sc.blob(c) for d, c in blobs.items()}
        day = time.strftime("%Y-%m-%d", time.gmtime())
        for det, n in sc.counts.items():
            self.store.execute("INSERT INTO pii_events VALUES (?,?,?,?) ON CONFLICT(project, day, detector) "
                               "DO UPDATE SET count = count + excluded.count", (project, day, det, n))
        return out

    def stats(self, project: str) -> List[Dict[str, Any]]:
        return self.store.query("SELECT day, detector, count FROM pii_events WHERE project=? ORDER BY day DESC, "
                                "count DESC", (project,))
