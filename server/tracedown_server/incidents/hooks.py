"""Notification hooks: Slack, PagerDuty, GitHub Issues and generic webhooks.

Hooks subscribe to events (``incident.opened``, ``incident.escalated``,
``incident.regressed``, ``drift.detected``) above a minimum severity. Payloads follow
each service's public API:

* Slack: incoming webhook, ``{"text", "blocks"}``
* PagerDuty: Events API v2, ``POST https://events.pagerduty.com/v2/enqueue`` with
  ``routing_key``, ``event_action``, ``dedup_key`` and ``payload``
* GitHub: ``POST https://api.github.com/repos/{owner}/{repo}/issues`` with a token

Delivery goes through a transport function so tests (and air-gapped installs) can
swap it. Every attempt is logged in ``hook_deliveries``.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS hooks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project       TEXT,
    kind          TEXT NOT NULL,
    name          TEXT,
    config        TEXT NOT NULL,
    events        TEXT NOT NULL,
    min_severity  TEXT NOT NULL DEFAULT 'medium',
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS hook_deliveries (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    hook_id   INTEGER NOT NULL,
    event     TEXT NOT NULL,
    at        INTEGER NOT NULL,
    ok        INTEGER NOT NULL,
    status    INTEGER,
    error     TEXT
);
"""

KINDS = ("slack", "pagerduty", "github", "webhook")
EVENTS = ("incident.opened", "incident.escalated", "incident.regressed", "drift.detected")
SEVERITY = ["none", "low", "medium", "high", "critical"]

Transport = Callable[[str, Dict[str, str], Dict[str, Any]], Tuple[int, str]]


def http_transport(url: str, headers: Dict[str, str], body: Dict[str, Any]) -> Tuple[int, str]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode(errors="replace")[:500]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:500]


# ----------------------------------------------------------------------------- payloads

def _link(base_url: str, kind: str, ident: Any) -> str:
    """A link into the web app: /incidents/<id>/ and /drift/?id=<id>."""
    if not base_url:
        return ""
    base = base_url.rstrip("/")
    return f"{base}/drift/?id={ident}" if kind == "drift" else f"{base}/{kind}/{ident}/"


def _summary(event: str, obj: Dict[str, Any]) -> str:
    if event.startswith("drift"):
        return f"[TDN drift · {obj['severity']}] {obj['headline']}"
    verb = {"incident.opened": "New incident", "incident.escalated": "Incident escalated",
            "incident.regressed": "Incident regressed"}[event]
    return f"[TDN {obj['severity']}] {verb} #{obj['id']}: {obj['title']} ({obj['run_count']} runs)"


def build_request(kind: str, config: Dict[str, Any], event: str, obj: Dict[str, Any],
                  base_url: str = "") -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    summary = _summary(event, obj)
    is_drift = event.startswith("drift")
    link = _link(base_url, "drift" if is_drift else "incidents", obj.get("id", ""))
    if kind == "slack":
        lines = [f"*{summary}*"]
        if not is_drift:
            lines += [f"*Root cause:* {obj['root_cause']}", f"*Suggested fix:* {obj.get('suggested_fix') or '-'}"]
        else:
            lines += [f"• {s}" for s in obj.get("signals", [])]
        if link:
            lines.append(f"<{link}|Open in Tracedown>")
        return config["url"], {}, {
            "text": summary,
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}],
        }
    if kind == "pagerduty":
        sev = {"low": "info", "medium": "warning", "high": "error", "critical": "critical"}.get(obj["severity"], "warning")
        dedup = f"tdn-drift-{obj.get('agent')}-{obj.get('dimension')}-{obj.get('candidate')}" if is_drift \
            else f"tdn-incident-{obj['id']}"
        body = {
            "routing_key": config["routing_key"], "event_action": "trigger", "dedup_key": dedup,
            "payload": {"summary": summary[:1024], "source": "agent-tracedown", "severity": sev,
                        "component": obj.get("agent") or "agent", "custom_details": {
                            k: obj.get(k) for k in ("root_cause", "suggested_fix", "run_count", "signals",
                                                    "category", "target") if obj.get(k) is not None}},
        }
        if link:
            body["links"] = [{"href": link, "text": "Tracedown"}]
        return config.get("url", "https://events.pagerduty.com/v2/enqueue"), {}, body
    if kind == "github":
        api = config.get("api_url", "https://api.github.com").rstrip("/")
        url = f"{api}/repos/{config['repo']}/issues"
        if is_drift:
            body_md = "\n".join([f"**{obj['headline']}**", ""] + [f"- {s}" for s in obj.get("signals", [])])
        else:
            body_md = "\n".join([
                f"**Root cause:** {obj['root_cause']}", "", f"**Suggested fix:** {obj.get('suggested_fix') or '-'}", "",
                f"**Affected runs:** {obj['run_count']} · **Severity:** {obj['severity']} · **Category:** {obj['category']}",
            ])
        if link:
            body_md += f"\n\n[Open in Tracedown]({link})"
        return url, {"Authorization": f"Bearer {config['token']}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"}, {
            "title": summary[:250], "body": body_md + "\n\n_Filed by Tracedown._",
            "labels": config.get("labels", ["agent-tracedown", "incident" if not is_drift else "drift"]),
        }
    if kind == "webhook":
        return config["url"], config.get("headers", {}), {"event": event, "summary": summary, "data": obj}
    raise ValueError(f"unknown hook kind {kind!r}")


# ----------------------------------------------------------------------------- registry and delivery

_SECRETISH = ("url", "key", "token", "secret", "password", "authorization", "auth", "header")


def _redact(value: Any, key: str = "") -> Any:
    """Mask every value under a secret-looking key, at any depth (headers included)."""
    if isinstance(value, dict):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, key) for v in value]
    if value and any(t in key.lower() for t in _SECRETISH):
        return "•••" + str(value)[-4:]
    return value


def check_destination(url: str) -> None:
    """Refuse hook URLs that resolve to private, loopback or link-local addresses (SSRF),
    unless TDN_HOOKS_ALLOW_PRIVATE=1 (e.g. an internal Slack proxy)."""
    import ipaddress
    import os
    import socket
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ValueError("hook URL must be http(s) with a host")
    if os.environ.get("TDN_HOOKS_ALLOW_PRIVATE") == "1":
        return
    if parsed.scheme != "https":
        raise ValueError("hook URL must use https")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return  # unresolvable now; checked again at send time
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError(f"hook URL resolves to a non-public address ({ip})")


def _destination(kind: str, config: Dict[str, Any]) -> Optional[str]:
    if kind in ("slack", "webhook"):
        return config.get("url")
    if kind == "github":
        return config.get("api_url", "https://api.github.com")
    if kind == "pagerduty":
        return config.get("url", "https://events.pagerduty.com/v2/enqueue")
    return None


def create(store, kind: str, config: Dict[str, Any], events: Optional[List[str]] = None,
           min_severity: str = "medium", project: Optional[str] = None, name: Optional[str] = None) -> Dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    required = {"slack": ["url"], "pagerduty": ["routing_key"], "github": ["repo", "token"], "webhook": ["url"]}[kind]
    missing = [k for k in required if not config.get(k)]
    if missing:
        raise ValueError(f"{kind} hook needs {', '.join(missing)}")
    dest = _destination(kind, config)
    if dest:
        check_destination(dest)
    events = events or list(EVENTS)
    bad = [e for e in events if e not in EVENTS]
    if bad or min_severity not in SEVERITY:
        raise ValueError(f"unknown event(s) {bad}" if bad else f"min_severity must be one of {SEVERITY}")
    hid = store.execute("INSERT INTO hooks(project, kind, name, config, events, min_severity, created_at) "
                        "VALUES (?,?,?,?,?,?,?)", (project, kind, name or kind, json.dumps(config),
                                                    json.dumps(events), min_severity, time.time_ns()))
    return get(store, hid)


def get(store, hook_id: int) -> Optional[Dict[str, Any]]:
    rows = store.query("SELECT * FROM hooks WHERE id=?", (hook_id,))
    return _public(rows[0]) if rows else None


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    r = dict(row)
    r["config"] = _redact(json.loads(r["config"]))
    r["events"] = json.loads(r["events"])
    r["enabled"] = bool(r["enabled"])
    return r


def list_hooks(store, project: Optional[str] = None) -> List[Dict[str, Any]]:
    q, args = "SELECT * FROM hooks", []
    if project:
        q += " WHERE project IS ? OR project IS NULL"
        args.append(project)
    return [_public(r) for r in store.query(q + " ORDER BY id", args)]


def delete(store, hook_id: int) -> None:
    store.execute("DELETE FROM hooks WHERE id=?", (hook_id,))


def deliveries(store, hook_id: Optional[int] = None, limit: int = 50) -> List[Dict[str, Any]]:
    if hook_id:
        return store.query("SELECT * FROM hook_deliveries WHERE hook_id=? ORDER BY id DESC LIMIT ?", (hook_id, limit))
    return store.query("SELECT * FROM hook_deliveries ORDER BY id DESC LIMIT ?", (limit,))


class Notifier:
    def __init__(self, store, transport: Transport = http_transport, base_url: str = "") -> None:
        self.store, self.transport, self.base_url = store, transport, base_url

    def notify(self, event: str, obj: Dict[str, Any], project: Optional[str] = None,
               only_hook: Optional[int] = None) -> List[Dict[str, Any]]:
        sev = obj.get("severity", "medium")
        results = []
        rows = self.store.query("SELECT * FROM hooks WHERE enabled=1" + (" AND id=?" if only_hook else ""),
                                (only_hook,) if only_hook else ())
        for h in rows:
            if h["project"] not in (None, project) and not only_hook:
                continue
            if not only_hook and (event not in json.loads(h["events"]) or
                                  SEVERITY.index(sev) < SEVERITY.index(h["min_severity"])):
                continue
            try:
                url, headers, body = build_request(h["kind"], json.loads(h["config"]), event, obj, self.base_url)
                if self.transport is http_transport:
                    check_destination(url)  # DNS may have changed since the hook was created
                status, text = self.transport(url, headers, body)
                ok = 200 <= status < 300
                err = None if ok else f"HTTP {status}"  # never echo the remote response body
            except Exception as exc:  # noqa: BLE001 - a broken hook must not break ingest
                status, ok, err = None, False, f"{type(exc).__name__}: {exc}"
            self.store.execute("INSERT INTO hook_deliveries(hook_id, event, at, ok, status, error) VALUES (?,?,?,?,?,?)",
                               (h["id"], event, time.time_ns(), int(ok), status, err))
            if obj.get("id") and event.startswith("incident"):
                from . import _event
                _event(self.store, obj["id"], "notified", {"hook": h["name"], "kind": h["kind"], "ok": ok})
            results.append({"hook_id": h["id"], "kind": h["kind"], "ok": ok, "status": status, "error": err})
        return results
