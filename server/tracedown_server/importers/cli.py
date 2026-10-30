"""``tracedown-import``: diagnose agent runs you already record elsewhere.

    # OpenTelemetry export (OTel Collector file exporter, Phoenix, OpenLLMetry, ...)
    tracedown-import otlp traces.json

    # Langfuse: every trace with an error in the last day, or specific traces
    export LANGFUSE_PUBLIC_KEY=pk-lf-... LANGFUSE_SECRET_KEY=sk-lf-...
    tracedown-import langfuse --errors-since 24h
    tracedown-import langfuse --trace-id 7f3a... --trace-id 9b21...
    tracedown-import langfuse-file trace.json     # a trace exported from the Langfuse UI or API

By default each run is analyzed locally and its stack trace printed; nothing leaves your
machine. ``--send http://localhost:7070`` also stores the runs in a Tracedown server
(UI, incidents, drift), and ``--save runs.jsonl`` writes them for later.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

Run = Tuple[str, List[Dict[str, Any]], Dict[str, str]]


def _since(text: str) -> datetime:
    m = re.fullmatch(r"(\d+)\s*([mhdw])", text.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
        return datetime.now(timezone.utc) - delta
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def explain(runs: List[Run], judge_spec: str = None, out=None) -> List[Dict[str, Any]]:
    from ..rca import analyze
    out = out or sys.stdout
    from ..rca.judge import from_spec
    judge = from_spec(judge_spec) if judge_spec else None
    reports = []
    for trace_id, spans, blobs in runs:
        rep = analyze(spans, blobs, judge=judge)
        reports.append(rep)
        root = next(s for s in spans if s["attributes"].get("tdn.run.root"))
        print(f"\n━━ {root['attributes'].get('gen_ai.agent.name', root['name'])} · trace {trace_id} · "
              f"{len(spans)} steps · {rep['outcome']}", file=out)
        if rep["outcome"] != "failure" and not rep["findings"]:
            print("   no failure recorded; pass --failed if this run went wrong", file=out)
            continue
        print(rep["stack_trace"], file=out)
        if rep["findings"]:
            print("   suspects:", file=out)
            for k, f in enumerate(rep["findings"][:3], 1):
                print(f"   {k}. step {f['step']:<4} {f['label']}: {f['title'][:110]}", file=out)
    return reports


def send(runs: List[Run], endpoint: str, api_key: str = None, project: str = "imported") -> None:
    from agent_tracedown.exporters import HttpExporter
    ex = HttpExporter(endpoint, api_key=api_key, project=project, timeout=30)
    for _, spans, blobs in runs:
        ex.export(spans, blobs)
        if ex.last_error:
            raise SystemExit(f"could not send to {endpoint}: {ex.last_error}")
    print(f"sent {len(runs)} run(s) to {endpoint} (project {project})")


def main(argv: List[str] = None) -> int:
    p = argparse.ArgumentParser(prog="tracedown-import", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="source", required=True)

    def common(sp):
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--failed", dest="failed", action="store_const", const=True, default=None,
                       help="treat every imported run as failed (e.g. runs a user flagged)")
        g.add_argument("--succeeded", dest="failed", action="store_const", const=False,
                       help="treat every imported run as successful")
        sp.add_argument("--send", metavar="URL", help="also store the runs in a Tracedown server")
        sp.add_argument("--api-key", help="Tracedown ingest key for --send")
        sp.add_argument("--project", default="imported")
        sp.add_argument("--save", metavar="FILE", help="write runs as JSON lines ({trace_id, spans, blobs})")
        sp.add_argument("--judge", help="LLM judge for weak findings, e.g. anthropic:claude-haiku-5-5")
        sp.add_argument("--quiet", action="store_true", help="don't print the analysis")

    o = sub.add_parser("otlp", help="OTLP/JSON file(s): OTel Collector file exporter, Phoenix, OpenLLMetry export")
    o.add_argument("files", nargs="+")
    common(o)
    lf = sub.add_parser("langfuse", help="fetch traces from Langfuse (cloud or self-hosted)")
    lf.add_argument("--trace-id", action="append", default=[])
    lf.add_argument("--errors-since", metavar="24h|7d|ISO", help="every trace with an ERROR observation since then")
    lf.add_argument("--limit", type=int, default=20, help="most traces to fetch with --errors-since")
    lf.add_argument("--host", help="default: $LANGFUSE_HOST or https://cloud.langfuse.com")
    lf.add_argument("--public-key")
    lf.add_argument("--secret-key")
    common(lf)
    lff = sub.add_parser("langfuse-file", help="Langfuse JSON export(s): a trace with observations, or observations")
    lff.add_argument("files", nargs="+")
    common(lff)
    a = p.parse_args(argv)

    runs: List[Run] = []
    if a.source == "otlp":
        from .otel import import_otlp, load_otlp_file
        for f in a.files:
            runs += import_otlp(load_otlp_file(f), failed=a.failed)
    elif a.source == "langfuse-file":
        from .langfuse import import_observations, load_file
        for f in a.files:
            obs, trace = load_file(f)
            runs += import_observations(obs, trace, failed=a.failed)
    else:
        from .langfuse import LangfuseClient, fetch_runs
        if not a.trace_id and not a.errors_since:
            p.error("langfuse: pass --trace-id ID (repeatable) or --errors-since 24h")
        client = LangfuseClient(a.host, a.public_key, a.secret_key)
        since = _since(a.errors_since) if a.errors_since else None
        # traces found through an ERROR observation failed by definition
        failed = a.failed if a.failed is not None else (True if since and not a.trace_id else None)
        runs = fetch_runs(client, a.trace_id, since, a.limit, failed=failed)

    if not runs:
        print("no runs found")
        return 1
    if a.save:
        with open(a.save, "w", encoding="utf-8") as fh:
            for tid, spans, blobs in runs:
                fh.write(json.dumps({"trace_id": tid, "spans": spans, "blobs": blobs}) + "\n")
        print(f"saved {len(runs)} run(s) to {a.save}")
    if a.send:
        send(runs, a.send, a.api_key, a.project)
    if not a.quiet:
        explain(runs, a.judge)
    return 0


if __name__ == "__main__":
    sys.exit(main())
