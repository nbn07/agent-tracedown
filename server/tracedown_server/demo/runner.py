"""Run the demo agents and show what Tracedown makes of each failure.

    tracedown up                                     # does this for you, then opens the web app
    python -m tracedown_server.demo                  # offline: print every diagnosis in the terminal
    python -m tracedown_server.demo --endpoint http://localhost:7070   # send the runs to a server

Offline, spans stay in memory and the root-cause engine runs in-process.
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request

import agent_tracedown as tdn

from . import agents as demo


def _get(endpoint, path, wait=60.0):
    """GET, retrying while a run is not processed yet: with Kafka (TDN_KAFKA_BOOTSTRAP) the server
    accepts spans at once and workers analyze them a moment later."""
    import time
    deadline = time.time() + wait
    while True:
        try:
            with urllib.request.urlopen(endpoint + path, timeout=10) as r:
                body = r.read().decode()
            return body if path.endswith("stacktrace") else json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code not in (404, 409) or time.time() > deadline:
                raise
            time.sleep(0.5)


def _post(endpoint, path, payload=None):
    req = urllib.request.Request(endpoint + path, data=json.dumps(payload or {}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def run_all():
    ids = {}
    for name, fn in demo.ALL.items():
        try:
            ids[name] = fn()
        except tdn.ToolError:
            ids[name] = last_trace_id[0]
        ids[name] = ids[name] or last_trace_id[0]
    return ids


last_trace_id = [None]


class _Tap(tdn.MemoryExporter):
    """Remembers the latest trace id, then forwards to the real exporter."""

    def __init__(self, inner):
        super().__init__()
        self.inner = inner

    def export(self, spans, blobs):
        super().export(spans, blobs)
        last_trace_id[0] = spans[0]["trace_id"]
        if self.inner:
            self.inner.export(spans, blobs)


def load(endpoint, verbose=True):
    """Send every demo run to ``endpoint`` (or keep them in memory when it is None).

    Returns ``(ids, tap, replay_id)``: the first run of each story, the exporter that saw
    every span, and the replayed calendar run."""
    say = print if verbose else (lambda *a, **k: None)
    tap = _Tap(tdn.HttpExporter(endpoint, project="demo") if endpoint else None)
    tdn.init(project="demo", exporter=tap)
    if endpoint:  # example prices so the analytics page can show cost (USD per million tokens; not real list prices)
        req = urllib.request.Request(endpoint + "/api/v1/settings/prices", method="PUT",
                                     data=json.dumps({"gpt-4o-mini": {"input": 0.15, "output": 0.6}}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    # Background first, so the stories end up at the top of the newest-first run list.
    say("drift demo: 40 SupportBot runs on prompt billing@v3, then 40 on billing@v4 …")
    for v in ("billing@v3", "billing@v4"):
        for i in range(40):
            demo.support_cohort(v, i)
    for _ in range(4):  # the same failure again: one incident, several runs
        try:
            demo.calendar_agent()
        except tdn.ToolError:
            pass
    ids = run_all()
    if endpoint:  # successful run: analysis is on demand
        _post(endpoint, f"/api/v1/runs/{ids['pricing']}/analyze")

    # Replay the calendar failure with the fix, reusing every recorded step before it.
    bundle = tdn.fetch_bundle(endpoint, ids["calendar"]) if endpoint else \
        {"spans": tap.trace(ids["calendar"]), "blobs": tap.blobs}
    with tdn.replay(bundle, mode="from-step", from_step=4):
        replay_id = demo.calendar_agent(extractor=demo.extract_event_fixed)
    if endpoint:  # try Tracedown's own suggested fixes, so the Fixes tab shows results
        stand_ins = {"calendar_create_event": demo.create_event}
        for story in ("calendar", "refund", "pricing", "injection"):
            for r in tdn.try_fixes(ids[story], agent=demo.ALL[story], tools=stand_ins, tries=1, endpoint=endpoint):
                say(f"fix for {story}: {r['verdict']}")
    return ids, tap, replay_id


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m tracedown_server.demo",
                                 description="Run the demo agents and print Tracedown's diagnosis of each.")
    ap.add_argument("--endpoint", help="Tracedown server URL, e.g. http://localhost:7070 (default: offline)")
    args = ap.parse_args(argv)
    endpoint = args.endpoint.rstrip("/") if args.endpoint else None
    ids, tap, new_id = load(endpoint)

    for name, tid in ids.items():
        print(f"\n━━━ {name}: run {tid[:8]} " + "━" * 40)
        if endpoint:
            print(_get(endpoint, f"/api/v1/runs/{tid}/stacktrace"))
        else:
            from tracedown_server.rca import analyze
            print(analyze(tap.trace(tid), tap.blobs)["stack_trace"])

    print("\n━━━ replay: calendar, steps before 4 recorded, extraction live with the fixed extractor " + "━" * 4)
    if endpoint:
        d = _get(endpoint, f"/api/v1/diff?a={ids['calendar']}&b={new_id}")
        print(f"original {d['a'][:8]} → {d['outcome_a']};  replay {d['b'][:8]} → {d['outcome_b']};  "
              f"first divergence at step {d['first_divergence']}")
        for row in d["steps"]:
            if row["change"] != "same":
                print(f"  step {row['step']:>2}  {row['change']:<8} {(row['b'] or row['a'])['name']}")
        reports = _get(endpoint, "/api/v1/drift/reports?agent=SupportBot")["reports"]
        for r in reports:
            print(f"drift report: {r['report']['headline']}")
        for inc in _get(endpoint, "/api/v1/incidents")["incidents"]:
            print(f"incident #{inc['id']} [{inc['severity']}] {inc['title']} · {inc['run_count']} runs")
        print(f"\nOpen {endpoint}/runs/{ids['calendar']} to explore.")
    else:
        root = [s for s in tap.trace(new_id) if not s["parent_span_id"]][0]
        a = root["attributes"]
        print(f"replay outcome: {a['tdn.run.outcome']}; recorded steps {a['tdn.replay.recorded_steps']}; "
              f"live steps {a['tdn.replay.live_steps']}")
        print("\nSee them in the web app: tracedown up")


if __name__ == "__main__":
    main()
