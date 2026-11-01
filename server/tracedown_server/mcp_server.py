"""Tracedown as an MCP server: ask your coding assistant why a run failed.

    tracedown mcp                                  # stdio, talks to http://127.0.0.1:7070
    tracedown mcp --endpoint https://tracedown.example.com --api-key-env TDN_API_KEY

Claude Code:  claude mcp add tracedown -- uvx --from "agent-tracedown-server[mcp]" tracedown mcp

The tools only read, except ``diagnose_trace``, which imports a pasted export into the
"pasted" project, the same as "Diagnose a trace" in the web app. Answers are plain text
written for a model to read: short, with step numbers and run ids it can pass back.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

DEFAULT_ENDPOINT = "http://127.0.0.1:7070"
MAX_PAYLOAD = 2000  # characters of a step's input or output shown to the model


class TracedownError(RuntimeError):
    pass


class Client:
    """The few HTTP calls the tools need. Errors become plain sentences for the model."""

    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, api_key: Optional[str] = None, timeout: float = 30):
        self.endpoint, self.api_key, self.timeout = endpoint.rstrip("/"), api_key, timeout

    def _req(self, method: str, path: str, body: Optional[bytes] = None, raw: bool = False) -> Any:
        req = urllib.request.Request(self.endpoint + path, data=body, method=method)
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except ValueError:
                pass
            raise TracedownError(f"Tracedown answered {e.code}: {detail}") from None
        except (urllib.error.URLError, OSError) as e:
            raise TracedownError(f"Cannot reach the Tracedown server at {self.endpoint} ({e}). "
                                 "Start it with `tracedown up`, or pass --endpoint.") from None
        return data if raw else json.loads(data)

    def get(self, path: str, **params: Any) -> Any:
        q = {k: v for k, v in params.items() if v is not None}
        return self._req("GET", path + (("?" + urllib.parse.urlencode(q)) if q else ""))

    def text(self, path: str) -> str:
        return self._req("GET", path, raw=True)

    def post(self, path: str, body: bytes, **params: Any) -> Any:
        q = {k: v for k, v in params.items() if v is not None}
        return self._req("POST", path + (("?" + urllib.parse.urlencode(q)) if q else ""), body)


# ----------------------------------------------------------------------------- the tools, as plain functions

def _clip(value: Any, n: int = MAX_PAYLOAD) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= n else text[:n] + f"… ({len(text) - n} more characters)"


def list_failed_runs(c: Client, limit: int = 10, agent: Optional[str] = None,
                     project: Optional[str] = None) -> str:
    limit = min(max(limit, 1), 50)
    runs = c.get("/api/v1/runs", outcome="failure", limit=limit * 4, agent=agent, project=project)["runs"]
    runs = [r for r in runs if not r.get("replay_of")][:limit]  # replays are the user's experiments
    if not runs:
        return "No failed runs" + (f" for agent {agent}" if agent else "") + "."
    lines = [f"{len(runs)} most recent failed runs (newest first):"]
    for r in runs:
        rc = r.get("root_cause") or {}
        fault = (r.get("fault") or {}).get("verdict")
        cause = f"{rc.get('label')} at step {rc.get('step')}" if rc else "not analyzed yet"
        lines.append(f"- {r['trace_id']}  {r.get('agent_name')}  [{r.get('project')}]  "
                     f"root cause: {cause}" + (f"; fault: {fault}" if fault else ""))
    return "\n".join(lines)


def explain_run(c: Client, trace_id: str) -> str:
    detail = c.get(f"/api/v1/runs/{trace_id}")
    report = detail.get("findings") or c.post(f"/api/v1/runs/{trace_id}/analyze", b"")
    run = detail.get("run") or {}
    out = [f"Run {trace_id}: {run.get('agent_name')}, outcome {run.get('outcome')}, "
           f"{run.get('span_count')} steps."]
    if run.get("replay_of"):
        out.append(f"This run is a replay of {run['replay_of']}.")
    rc = report.get("root_cause")
    if not rc:
        out.append("Tracedown found no root cause: nothing in the run looks wrong.")
        return "\n".join(out)
    fault = report.get("fault") or {}
    out += ["", "AI stack trace:", report.get("stack_trace", "").rstrip(), "",
            f"Top suspect: {rc['label']} at step {rc['step']}: {rc['title']}",
            *[f"  evidence: {e}" for e in rc.get("evidence", [])]]
    if fault:
        out.append(f"Who was at fault: {fault.get('verdict')} ({fault.get('reason')})")
    others = [f for f in report.get("findings", []) if f.get("id") != rc.get("id") and not f.get("symptom")][:2]
    if others:
        out.append("Next suspects: " + "; ".join(f"{f['label']} at step {f['step']}" for f in others))
    out += ["", "The ranking is a first place to look, not a certainty: check the evidence. "
            "Use get_step to read a step, and suggest_fixes for what to change."]
    return "\n".join(out)


def get_step(c: Client, trace_id: str, step: int) -> str:
    bundle = c.get(f"/api/v1/runs/{trace_id}/bundle")
    spans = [s for s in bundle["spans"] if s["attributes"].get("tdn.step") == step]
    if not spans:
        steps = sorted(s["attributes"].get("tdn.step", 0) for s in bundle["spans"])
        return f"Run {trace_id} has no step {step}. Steps: {steps[0]}–{steps[-1]}."
    s, blobs = spans[0], bundle.get("blobs", {})

    def payload(d: Optional[str]) -> str:
        if not d:
            return "(none)"
        if d not in blobs:
            return "(not stored: content capture was off or redacted)"
        return _clip(blobs[d])

    a = s["attributes"]
    keep = {k: v for k, v in a.items() if k.startswith(("gen_ai.", "tdn.tool.", "tdn.retrieval.", "tdn.memory.",
                                                           "tdn.agent", "tdn.replay."))}
    status = s.get("status") or {}
    return "\n".join([
        f"Step {step} of run {trace_id}: {s['name']} ({s['kind']})",
        f"status: {status.get('code', 'UNSET')}" + (f": {status['message']}" if status.get("message") else ""),
        f"attributes: {_clip(keep, 800)}",
        f"input: {payload(s.get('input_digest'))}",
        f"output: {payload(s.get('output_digest'))}",
    ])


def suggest_fixes(c: Client, trace_id: str) -> str:
    data = c.get(f"/api/v1/runs/{trace_id}/suggestions")
    sugg = data.get("suggestions") or []
    if not sugg:
        return "No suggestions: Tracedown found no root cause in this run."
    status = {"works": "WORKS", "does_not_work": "DID NOT WORK", "untested": "not tested yet",
              "not_tried": "needs a code change"}
    out = [f"Suggested fixes for run {trace_id}, most likely first:"]
    for s in sugg:
        line = f"{s['id']}. [{status.get(s.get('status'), s.get('status'))}" + \
               (f", {s['result']}" if s.get("result") else "") + f"] {s['label']} at step {s['step']}"
        out += ["", line, f"   change: {s['change']}"]
        out.append(f"   Tracedown can try it: {s['tries_as']}" if s.get("tries_as")
                   else f"   not tried automatically: {s.get('why_not_tried')}")
        for r in (s.get("replays") or [])[:3]:
            out.append(f"   replay {r['trace_id']}: {'passed' if r['check']['passed'] else 'failed'} "
                       f"({'; '.join(r['check']['reasons'])})")
    if any(s.get("try") for s in sugg):
        out += ["", "To try them on the user's agent (Python), run where the agent runs:",
                f"   tdn.try_fixes(\"{trace_id}\", agent=lambda: my_agent(...), tools={{...stand-ins...}})",
                "No tool runs for real during the check unless it is given a stand-in or listed in live_tools."]
    return "\n".join(out)


def check_replay(c: Client, trace_id: str, replay_id: str) -> str:
    r = c.get(f"/api/v1/runs/{trace_id}/fix-check", replay=replay_id)
    head = {"fixed": "Fixed", "passes_tracedown_checks": "Passes Tracedown's checks",
            "not_fixed": "Not fixed", "nothing_to_fix": "Nothing to fix"}.get(r.get("verdict"), r.get("verdict"))
    return f"{head}: replay {replay_id} of run {trace_id}.\n" + "\n".join(f"- {x}" for x in r.get("reasons", []))


def check_claims(c: Client, trace_id: str) -> str:
    data = c.get(f"/api/v1/runs/{trace_id}/claims")
    claims = data.get("claims") or []
    if not claims:
        return "The final answer has no checkable claims."
    out = [f"Claims in the final answer of run {trace_id}:"]
    for cl in claims[:20]:
        if cl.get("label") == "no_claim":
            continue
        ev = (cl.get("evidence") or [None])[0]
        where = f" (best source: {ev.get('source') or ev.get('unit') or ev.get('kind')}, step {ev.get('step')})" \
            if ev else " (no source)"
        why = f": {cl['rationale']}" if cl.get("rationale") else ""
        out.append(f"- [{cl.get('label')}] {cl.get('text')}{where}{why}")
    return "\n".join(out)


def list_incidents(c: Client, status: str = "open", project: Optional[str] = None) -> str:
    incs = c.get("/api/v1/incidents", status=status or None, project=project)["incidents"][:15]
    if not incs:
        return f"No {status} incidents."
    out = [f"{len(incs)} {status} incidents, most severe first (one incident = failed runs with the same cause):"]
    for i in incs:
        out.append(f"- #{i['id']} [{i['severity']}] {i['title']} ({i['run_count']} runs, agent {i.get('agent')})"
                   + (f"\n    fix: {i['suggested_fix']}" if i.get("suggested_fix") else ""))
    return "\n".join(out)


def diagnose_trace(c: Client, export: str) -> str:
    try:
        json.loads(export)
    except ValueError:
        pass  # JSON lines are fine too; the server decides
    r = c.post("/api/v1/import", export.encode("utf-8"))
    if not r.get("runs"):
        return "The export held no runs."
    out = [f"Imported {len(r['runs'])} run(s) from a {r['format']} export:"]
    for x in r["runs"]:
        cause = f"{x['root_cause']} at step {x['step']}" if x.get("root_cause") else "no root cause found"
        out.append(f"- {x['trace_id']} {x.get('agent')} ({x.get('outcome')}): {cause}")
    out.append("Use explain_run with a trace id for the full diagnosis. Imported runs cannot be replayed.")
    return "\n".join(out)


# ----------------------------------------------------------------------------- MCP wiring

INSTRUCTIONS = (
    "Tracedown finds the step that caused an AI agent's failure, shows the evidence, and suggests fixes "
    "it can test by replaying the run. Start with list_failed_runs or explain_run. Verdicts are ranked "
    "suspects, not certainties: quote the evidence, and read the step with get_step before you conclude."
)


def build(client: Client):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - exercised by hand
        raise SystemExit('The MCP server needs the "mcp" package: pip install "agent-tracedown-server[mcp]"') from exc

    server = FastMCP("tracedown", instructions=INSTRUCTIONS)

    def wrap(fn, *args, **kwargs) -> str:
        try:
            return fn(client, *args, **kwargs)
        except TracedownError as e:
            return str(e)

    @server.tool(name="list_failed_runs")
    def list_failed_runs_tool(limit: int = 10, agent: Optional[str] = None, project: Optional[str] = None) -> str:
        """List recent failed agent runs with Tracedown's root cause for each. Filter by agent name or project."""
        return wrap(list_failed_runs, limit, agent, project)

    @server.tool(name="explain_run")
    def explain_run_tool(trace_id: str) -> str:
        """Why did this run fail? The AI stack trace, the top suspect step with evidence, who was at fault
        (agent or user), and the next suspects."""
        return wrap(explain_run, trace_id)

    @server.tool(name="get_step")
    def get_step_tool(trace_id: str, step: int) -> str:
        """Read one step of a run: its kind, status, attributes, input and output (shortened)."""
        return wrap(get_step, trace_id, step)

    @server.tool(name="suggest_fixes")
    def suggest_fixes_tool(trace_id: str) -> str:
        """What to change to fix a failed run, most likely first, and whether each fix has been tried
        and worked (from replays checked by Tracedown)."""
        return wrap(suggest_fixes, trace_id)

    @server.tool(name="check_replay")
    def check_replay_tool(trace_id: str, replay_id: str) -> str:
        """Did a replay fix the run? Checks that the blamed problem is gone, nothing new broke and the
        replay finished."""
        return wrap(check_replay, trace_id, replay_id)

    @server.tool(name="check_claims")
    def check_claims_tool(trace_id: str) -> str:
        """Check each sentence of the run's final answer against its sources: supported, unsupported,
        contradicted or stale."""
        return wrap(check_claims, trace_id)

    @server.tool(name="list_incidents")
    def list_incidents_tool(status: str = "open", project: Optional[str] = None) -> str:
        """Recurring problems: failed runs grouped by the same root cause, most severe first, with a fix."""
        return wrap(list_incidents, status, project)

    @server.tool(name="diagnose_trace")
    def diagnose_trace_tool(export: str) -> str:
        """Diagnose a trace export the user pasted: Langfuse, OpenTelemetry (OTLP/JSON) or a Tracedown
        bundle. Imports it into the "pasted" project and returns the root cause of each run."""
        return wrap(diagnose_trace, export)

    return server


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="tracedown mcp", description="Run Tracedown as an MCP server (stdio).")
    ap.add_argument("--endpoint", default=os.environ.get("TDN_ENDPOINT", DEFAULT_ENDPOINT),
                    help=f"Tracedown server URL (default: $TDN_ENDPOINT or {DEFAULT_ENDPOINT})")
    ap.add_argument("--api-key-env", default="TDN_API_KEY",
                    help="name of the environment variable that holds the API key (default: TDN_API_KEY)")
    args = ap.parse_args(argv)
    build(Client(args.endpoint, os.environ.get(args.api_key_env))).run("stdio")
    return 0
