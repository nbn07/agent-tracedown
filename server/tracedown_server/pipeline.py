"""The ingest pipeline: what happens to spans after they arrive.

The HTTP API calls it inline (dev profile); the queue workers in
``tracedown_server.queue`` call the same code when ingest goes through Kafka/Redpanda.

For each batch: apply the project's PII policy to payloads, store spans, publish
live events, and for every run whose root span arrived (the run is finished):
analyse it, store its profile, attach it to an incident, scan for drift, and
notify hooks.
"""
from __future__ import annotations

import hashlib
import os
import threading
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

from . import drift, incidents
from .incidents import hooks
from .claims import investigate
from .rca import RunView, analyze


class EventBus:
    """In-process pub/sub for live views (Server-Sent Events). Bounded per subscriber."""

    def __init__(self, max_queue: int = 1000) -> None:
        self._subs: Dict[int, List[Dict[str, Any]]] = {}
        self._cv = threading.Condition()
        self._next = 0
        self.max_queue = max_queue

    def subscribe(self) -> int:
        with self._cv:
            self._next += 1
            self._subs[self._next] = []
            return self._next

    def unsubscribe(self, sid: int) -> None:
        with self._cv:
            self._subs.pop(sid, None)

    def publish(self, event: Dict[str, Any]) -> None:
        with self._cv:
            for q in self._subs.values():
                q.append(event)
                del q[:-self.max_queue]
            self._cv.notify_all()

    def drain(self, sid: int, timeout: float = 15.0) -> List[Dict[str, Any]]:
        with self._cv:
            if not self._subs.get(sid):
                self._cv.wait(timeout)
            items, self._subs[sid] = self._subs.get(sid, []), []
            return items


class Pipeline:
    def __init__(self, store, notifier: Optional[hooks.Notifier] = None, bus: Optional[EventBus] = None,
                 pii: Optional[Callable[[str, Dict[str, str]], Dict[str, str]]] = None,
                 drift_min_runs: Optional[int] = None) -> None:
        self.store = store
        store.ensure_schema(drift.SCHEMA + incidents.SCHEMA + hooks.SCHEMA)
        self.notifier = notifier or hooks.Notifier(store, base_url=os.environ.get("TDN_PUBLIC_URL", ""))
        self.bus = bus or EventBus()
        self.pii = pii
        self.drift_min_runs = drift_min_runs or int(os.environ.get("TDN_DRIFT_MIN_RUNS", "20"))
        url = os.environ.get("TDN_OTLP_FORWARD")
        self.forward = otlp_forwarder(url) if url else None

    # ------------------------------------------------------------------ blobs
    def process_blobs(self, project: str, blobs: Dict[str, str]) -> int:
        bad = [d for d, c in blobs.items() if d != "sha256:" + hashlib.sha256(c.encode("utf-8")).hexdigest()]
        if bad:
            # A digest must name its content; otherwise a client could plant content under
            # a digest another run references.
            raise ValueError(f"{len(bad)} blob(s) do not match their digest, e.g. {bad[0][:20]}…")
        if self.pii:
            blobs = self.pii(project, blobs)
        return self.store.put_blobs(blobs, project)

    # ------------------------------------------------------------------ spans
    def process_spans(self, project: str, spans: List[Dict[str, Any]]) -> Dict[str, Any]:
        n = self.store.put_spans(project, spans)
        by_trace: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for s in spans:
            by_trace[s["trace_id"]].append(s)
        for tid, batch in by_trace.items():
            self.bus.publish({"type": "spans", "trace_id": tid, "project": project,
                              "spans": [{k: s.get(k) for k in ("span_id", "parent_span_id", "name", "kind",
                                                              "start_time", "end_time", "status")} |
                                        {"step": s.get("attributes", {}).get("tdn.step")} for s in batch]})
        finished = [s["trace_id"] for s in spans if not s.get("parent_span_id")]
        result = {"accepted": n, "analyzed": [], "incidents": [], "drift": []}
        for tid in finished:
            self._finish_run(project, tid, result)
        return result

    def _finish_run(self, project: str, trace_id: str, result: Dict[str, Any]) -> None:
        spans = self.store.get_spans(trace_id)
        run = self.store.get_run(trace_id)
        blobs = self.store.get_blobs([d for s in spans for d in (s["input_digest"], s["output_digest"])], project)
        report = analyze(spans, blobs)
        if report["findings"]:
            self.store.put_findings(trace_id, report, spans[0]["start_time"])
            result["analyzed"].append(trace_id)
        view = RunView(spans, blobs)
        claim_report = investigate(view) if view.output(view.root) is not None else None
        self.store.set_profile(trace_id, drift.build_profile(spans, report, claim_report))
        if self.forward:
            self.forward(spans)
        self.bus.publish({"type": "run", "trace_id": trace_id, "project": project, "outcome": run["outcome"],
                          "agent": run["agent_name"],
                          "root_cause": (report.get("root_cause") or {}).get("label")})
        if run["replay_of"]:
            return  # replays are experiments: no incidents, no drift

        rc = report.get("root_cause")
        if rc and (run["outcome"] == "failure" or rc["category"] in incidents.SILENT):
            rec = incidents.record(self.store, project, report, spans, at_ns=run["started_at"])
            if rec:
                result["incidents"].append(rec["incident"]["id"])
                for ev in rec["events"]:
                    self.notifier.notify(ev, rec["incident"], project)

        self._maybe_scan_drift(project, run["agent_name"], trace_id, result)

    def _maybe_scan_drift(self, project: str, agent: str, trace_id: str, result: Dict[str, Any]) -> None:
        prof = self.store.get_run(trace_id)["profile"] or {}
        m = self.drift_min_runs
        should = False
        for dim, val in (prof.get("dims") or {}).items():
            if val is None:
                continue
            vals = drift.values(self.store, agent, dim, project)
            if len(vals) >= 2 and vals[-1]["value"] == val and vals[-1]["runs"] >= m and \
                    (vals[-1]["runs"] == m or vals[-1]["runs"] % 10 == 0):
                should = True
        if not should:
            return
        for rep in drift.scan(self.store, agent, project, min_runs=m):
            result["drift"].append(f"{rep['dimension']}:{rep['baseline']}->{rep['candidate']}:{rep['verdict']}")
            if rep["verdict"] == "drift" and rep.get("previous_verdict") != "drift":
                self.notifier.notify("drift.detected", rep, project)
                self.bus.publish({"type": "drift", "agent": agent, "headline": rep["headline"], "project": project})


def otlp_forwarder(url: str, headers: Optional[Dict[str, str]] = None):
    """Forward finished runs to an OpenTelemetry collector, Jaeger, Grafana Tempo or the
    Datadog Agent over OTLP/HTTP JSON (``TDN_OTLP_FORWARD=http://collector:4318/v1/traces``)."""
    import json
    import sys
    import urllib.request

    hdrs = {"Content-Type": "application/json", **(headers or json.loads(os.environ.get("TDN_OTLP_HEADERS", "{}")))}

    def send(spans: List[Dict[str, Any]]) -> None:
        from .otlp import to_otlp_json
        req = urllib.request.Request(url, data=json.dumps(to_otlp_json(spans)).encode(), headers=hdrs, method="POST")
        try:
            urllib.request.urlopen(req, timeout=5).read()
        except OSError as exc:  # forwarding is best effort
            print(f"[tdn] OTLP forward failed: {exc}", file=sys.stderr)

    return send
