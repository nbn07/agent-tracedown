"""Tracedown as an MCP server: ask your coding assistant why a run failed.

    tracedown mcp                                  # stdio, talks to http://127.0.0.1:7070
    tracedown mcp --endpoint https://tracedown.example.com      # key from $TDN_API_KEY

Claude Code:  claude mcp add tracedown -- uvx --from "agent-tracedown-server[mcp]" tracedown mcp

No tool changes a run. ``diagnose_trace`` imports a pasted export into the "pasted" project,
the same as "Diagnose a trace" in the web app, and ``explain_run`` asks the server to analyse a
run that has not been analysed yet. Answers are plain text written for a model to read: short,
with step numbers and run ids it can pass back. Failures are tool errors with a plain sentence.
Works with the mcp package 1.x (FastMCP) and 2.x (MCPServer).
"""
from __future__ import annotations

import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

DEFAULT_ENDPOINT = "http://127.0.0.1:7070"
MAX_PAYLOAD = 2000  # characters of a step's input or output shown to the model
KEY_HINT = ("Set TDN_API_KEY (or the variable named by --api-key-env) in the MCP client's env "
            "to an API key for this server.")


class TracedownError(RuntimeError):
    pass


_UNSAFE = re.compile(r"[/\\?#%\s\x00-\x1f\x7f]")


def _q(value: Any) -> str:
    """One path segment. An id like 'abc/claims' or 'abc?x=1' is refused instead of reaching
    another endpoint: the server decodes %2F back into a slash before routing, so quoting alone
    is not enough. Anything else (Langfuse allows custom ids) is quoted."""
    text = str(value)
    if not text or text in (".", "..") or _UNSAFE.search(text):
        raise TracedownError(f"{text[:80]!r} is not a Tracedown run id: it may not contain / \\ ? # % or spaces")
    return urllib.parse.quote(text, safe="")


def check_endpoint(endpoint: str) -> str:
    try:
        u = urllib.parse.urlparse(endpoint)
    except ValueError:
        u = None
    if u is None or u.scheme not in ("http", "https") or not u.netloc:
        raise TracedownError(f"--endpoint must be an http(s) URL such as {DEFAULT_ENDPOINT}, not {endpoint!r}")
    return endpoint.rstrip("/")


class _KeepKeyOnSameHost(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but send the API key only to the same host and never from https to http
    (a proxy upgrading http to https keeps working; an identity provider elsewhere gets no key)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        key = req.unredirected_hdrs.get("Authorization")
        if new is not None and key:
            old_u, new_u = urllib.parse.urlparse(req.full_url), urllib.parse.urlparse(newurl)
            if old_u.hostname == new_u.hostname and not (old_u.scheme == "https" and new_u.scheme == "http"):
                new.add_unredirected_header("Authorization", key)
        return new


_OPENER = urllib.request.build_opener(_KeepKeyOnSameHost)


class Client:
    """The few HTTP calls the tools need. Every failure becomes a TracedownError with a plain sentence."""

    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, api_key: Optional[str] = None, timeout: float = 30):
        self.endpoint, self.api_key, self.timeout = check_endpoint(endpoint), api_key, timeout

    def _req(self, method: str, path: str, body: Optional[bytes] = None) -> Any:
        try:
            req = urllib.request.Request(self.endpoint + path, data=body, method=method)
        except ValueError as e:
            raise TracedownError(f"Bad request URL: {e}") from None
        if self.api_key:
            # Unredirected: the key must not follow a redirect to another host (an auth proxy, say).
            req.add_unredirected_header("Authorization", f"Bearer {self.api_key}")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with _OPENER.open(req, timeout=self.timeout) as r:
                data = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except (ValueError, AttributeError):
                pass
            text = f"Tracedown answered {e.code}: {str(detail).rstrip()}"
            if e.code == 401:
                text = text.rstrip(".") + ". " + KEY_HINT
            raise TracedownError(text) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
            raise TracedownError(f"Cannot reach the Tracedown server at {self.endpoint} ({e}). "
                                 "Start it with `tracedown up`, or pass --endpoint.") from None
        try:
            return json.loads(data)
        except ValueError:
            raise TracedownError(f"{self.endpoint} did not answer like a Tracedown server (the reply is not "
                                 "JSON). Check --endpoint.") from None

    def get(self, path: str, **params: Any) -> Any:
        q = {k: v for k, v in params.items() if v is not None}
        return self._req("GET", path + (("?" + urllib.parse.urlencode(q)) if q else ""))

    def post(self, path: str, body: bytes, **params: Any) -> Any:
        q = {k: v for k, v in params.items() if v is not None}
        return self._req("POST", path + (("?" + urllib.parse.urlencode(q)) if q else ""), body)


# ----------------------------------------------------------------------------- the tools, as plain functions

def _clip(value: Any, n: int = MAX_PAYLOAD) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= n else text[:n] + f"… ({len(text) - n} more characters)"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def list_failed_runs(c: Client, limit: int = 10, agent: Optional[str] = None,
                     project: Optional[str] = None) -> str:
    limit = min(max(int(limit), 1), 50)
    runs = c.get("/api/v1/runs", outcome="failure", limit=limit * 4, agent=agent, project=project)["runs"]
    runs = [r for r in runs if not r.get("replay_of")][:limit]  # replays are the user's experiments
    if not runs:
        return "No failed runs" + (f" for agent {agent}" if agent else "") + "."
    lines = [f"{_plural(len(runs), 'most recent failed run')} (newest first):"]
    for r in runs:
        rc = r.get("root_cause") or {}
        fault = (r.get("fault") or {}).get("verdict")
        cause = f"{rc.get('label')} at step {rc.get('step')}" if rc else "not analysed yet"
        lines.append(f"- {r.get('trace_id')}  {r.get('agent_name')}  [{r.get('project')}]  "
                     f"root cause: {cause}" + (f"; fault: {fault}" if fault else ""))
    return "\n".join(lines)


def explain_run(c: Client, trace_id: str) -> str:
    detail = c.get(f"/api/v1/runs/{_q(trace_id)}")
    run = detail.get("run") or {}
    report = detail.get("findings")
    if not report:
        try:
            report = c.post(f"/api/v1/runs/{_q(trace_id)}/analyze", b"")
        except TracedownError as e:
            if "403" not in str(e):
                raise
            report = {}  # a key that may not run analysis: say so below
    out = [f"Run {trace_id}: {run.get('agent_name')}, outcome {run.get('outcome')}, "
           f"{_plural(run.get('span_count') or 0, 'step')}."]
    if run.get("replay_of"):
        out.append(f"This run is a replay of {run['replay_of']}.")
    rc = report.get("root_cause")
    if not report:
        out.append("This run has not been analysed, and this API key may not run the analysis.")
        return "\n".join(out)
    if not rc:
        out.append("Tracedown found no root cause: nothing in the run looks wrong.")
        return "\n".join(out)
    fault = report.get("fault") or {}
    out += ["", "AI stack trace:", (report.get("stack_trace") or "").rstrip(), "",
            f"Top suspect: {rc.get('label')} at step {rc.get('step')}: {rc.get('title')}",
            *[f"  evidence: {e}" for e in rc.get("evidence") or []]]
    if fault:
        out.append(f"Who was at fault: {fault.get('verdict')} ({fault.get('reason')})")
    others = [f for f in report.get("findings") or [] if f.get("id") != rc.get("id") and not f.get("symptom")][:2]
    if others:
        out.append("Next suspects: " + "; ".join(f"{f.get('label')} at step {f.get('step')}" for f in others))
    out += ["", "The ranking is a first place to look, not a certainty: check the evidence. "
            "Use get_step to read a step, and suggest_fixes for what to change."]
    return "\n".join(out)


def get_step(c: Client, trace_id: str, step: int) -> str:
    detail = c.get(f"/api/v1/runs/{_q(trace_id)}")
    spans = detail.get("spans") or []
    steps = sorted({s["attributes"].get("tdn.step") for s in spans if isinstance(s["attributes"].get("tdn.step"), int)})
    match = [s for s in spans if s["attributes"].get("tdn.step") == step]
    if not match:
        return f"Run {trace_id} has no step {step}." + (f" Steps: {steps[0]}–{steps[-1]}." if steps else "")
    s = match[0]

    def payload(d: Optional[str]) -> str:
        if not d:
            return "(none)"
        try:
            return _clip(c.get(f"/api/v1/blobs/{_q(d)}", trace_id=trace_id).get("content"))
        except TracedownError as e:
            return "(not stored: content capture was off or redacted)" if "404" in str(e) else f"({e})"

    a = s.get("attributes") or {}
    keep = {k: v for k, v in a.items() if k.startswith(("gen_ai.", "tdn.tool.", "tdn.retrieval.", "tdn.memory.",
                                                           "tdn.agent", "tdn.replay.", "tdn.error"))}
    status = s.get("status") or {}
    return "\n".join([
        f"Step {step} of run {trace_id}: {s.get('name')} ({s.get('kind')})",
        f"status: {status.get('code', 'UNSET')}" + (f": {status['message']}" if status.get("message") else ""),
        f"attributes: {_clip(keep, 800)}",
        f"input: {payload(s.get('input_digest'))}",
        f"output: {payload(s.get('output_digest'))}",
    ])


STATUS = {"works": "WORKS", "does_not_work": "DID NOT WORK", "untested": "not tested yet",
          "not_tried": "needs a code change"}


def suggest_fixes(c: Client, trace_id: str) -> str:
    sugg = c.get(f"/api/v1/runs/{_q(trace_id)}/suggestions").get("suggestions") or []
    if not sugg:
        return "No suggestions: Tracedown found no root cause in this run."
    out = [f"Suggested fixes for run {trace_id}, most likely first:"]
    for s in sugg:
        line = f"{s.get('id')}. [{STATUS.get(s.get('status'), s.get('status'))}" + \
               (f", {s['result']}" if s.get("result") else "") + f"] {s.get('label')} at step {s.get('step')}"
        out += ["", line, f"   change: {s.get('change')}"]
        out.append(f"   Tracedown can try it: {s['tries_as']}" if s.get("tries_as")
                   else f"   not tried automatically: {s.get('why_not_tried')}")
        for r in (s.get("replays") or [])[:3]:
            chk = r.get("check") or {}
            out.append(f"   replay {r.get('trace_id')}: {'passed' if chk.get('passed') else 'failed'} "
                       f"({'; '.join(chk.get('reasons') or [])})")
    if any(s.get("try") for s in sugg):
        out += ["", "To try them on the user's agent (Python), run where the agent runs:",
                f"   tdn.try_fixes(\"{trace_id}\", agent=lambda: my_agent(...), tools={{...stand-ins...}})",
                "No tool runs for real during the check unless it is given a stand-in or listed in live_tools."]
    return "\n".join(out)


def check_replay(c: Client, trace_id: str, replay_id: str) -> str:
    r = c.get(f"/api/v1/runs/{_q(trace_id)}/fix-check", replay=replay_id)
    head = {"fixed": "Fixed", "passes_tracedown_checks": "Passes Tracedown's checks",
            "not_fixed": "Not fixed", "nothing_to_fix": "Nothing to fix"}.get(r.get("verdict"), r.get("verdict"))
    return f"{head}: replay {replay_id} of run {trace_id}.\n" + "\n".join(f"- {x}" for x in r.get("reasons") or [])


def check_claims(c: Client, trace_id: str) -> str:
    claims = [cl for cl in c.get(f"/api/v1/runs/{_q(trace_id)}/claims").get("claims") or []
              if cl.get("label") != "no_claim"]
    if not claims:
        return "The final answer has no checkable claims."
    out = [f"Claims in the final answer of run {trace_id}:"]
    for cl in claims[:20]:
        ev = (cl.get("evidence") or [None])[0]
        where = f" (best source: {ev.get('source') or ev.get('unit') or ev.get('kind')}, step {ev.get('step')})" \
            if ev else " (no source)"
        why = f": {cl['rationale']}" if cl.get("rationale") else ""
        out.append(f"- [{cl.get('label')}] {cl.get('text')}{where}{why}")
    if len(claims) > 20:
        out.append(f"… and {len(claims) - 20} more.")
    return "\n".join(out)


def list_incidents(c: Client, status: str = "open", project: Optional[str] = None) -> str:
    status = (status or "all").strip().lower()
    incs = c.get("/api/v1/incidents", status=None if status == "all" else status, project=project)["incidents"][:15]
    which = "" if status == "all" else f"{status} "
    if not incs:
        return f"No {which}incidents."
    out = [f"{_plural(len(incs), which + 'incident')}, most severe first (one incident = failed runs with the "
           "same cause):"]
    for i in incs:
        out.append(f"- #{i.get('id')} [{i.get('severity')}] {i.get('title')} "
                   f"({_plural(i.get('run_count') or 0, 'run')}, agent {i.get('agent')})"
                   + (f"\n    fix: {i['suggested_fix']}" if i.get("suggested_fix") else ""))
    return "\n".join(out)


FORMATS = {"langfuse": "a Langfuse", "otlp": "an OpenTelemetry (OTLP)", "tracedown": "a Tracedown"}


def diagnose_trace(c: Client, export: str) -> str:
    r = c.post("/api/v1/import", export.encode("utf-8"))
    runs = r.get("runs") or []
    if not runs:
        return "The export held no runs."
    out = [f"Imported {_plural(len(runs), 'run')} from {FORMATS.get(r.get('format'), 'an')} export:"]
    for x in runs:
        cause = f"{x['root_cause']} at step {x.get('step')}" if x.get("root_cause") else "no root cause found"
        out.append(f"- {x.get('trace_id')} {x.get('agent')} ({x.get('outcome')}): {cause}")
    if r.get("skipped"):
        out.append(f"{_plural(r['skipped'], 'more run')} skipped (the limit per import).")
    out.append("Use explain_run with a trace id for the full diagnosis. Imported runs cannot be replayed.")
    return "\n".join(out)


# ----------------------------------------------------------------------------- MCP wiring

INSTRUCTIONS = (
    "Tracedown finds the step that caused an AI agent's failure, shows the evidence, and suggests fixes "
    "it can test by replaying the run. Start with list_failed_runs or explain_run. Verdicts are ranked "
    "suspects, not certainties: quote the evidence, and read the step with get_step before you conclude."
)


def _mcp():
    """(server class, ToolError, ToolAnnotations) for mcp 1.x (FastMCP) or 2.x (MCPServer)."""
    try:
        from mcp.server.fastmcp import FastMCP as Server
        from mcp.server.fastmcp.exceptions import ToolError
    except ImportError:
        try:
            from mcp.server.mcpserver import MCPServer as Server
            from mcp.server.mcpserver.exceptions import ToolError
        except ImportError:
            raise SystemExit('The MCP server needs the "mcp" package: '
                             'pip install "agent-tracedown-server[mcp]"') from None
    from mcp.types import ToolAnnotations
    return Server, ToolError, ToolAnnotations


def build(client: Client):
    Server, ToolError, ToolAnnotations = _mcp()
    server = Server("tracedown", instructions=INSTRUCTIONS, log_level="WARNING")
    read = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

    def wrap(fn, *args, **kwargs) -> str:
        try:
            return fn(client, *args, **kwargs)
        except TracedownError as e:
            raise ToolError(str(e)) from None
        except Exception as e:  # noqa: BLE001 - never a traceback to the client
            raise ToolError(f"Tracedown sent something unexpected ({type(e).__name__}: {e}). "
                            "Is the server the same version as this MCP server?") from None

    @server.tool(name="list_failed_runs", annotations=read)
    def list_failed_runs_tool(limit: int = 10, agent: Optional[str] = None, project: Optional[str] = None) -> str:
        """List recent failed agent runs with Tracedown's root cause for each. Filter by agent name or project."""
        return wrap(list_failed_runs, limit, agent, project)

    @server.tool(name="explain_run", annotations=read)
    def explain_run_tool(trace_id: str) -> str:
        """Why did this run fail? The AI stack trace, the top suspect step with evidence, who was at fault
        (agent or user), and the next suspects."""
        return wrap(explain_run, trace_id)

    @server.tool(name="get_step", annotations=read)
    def get_step_tool(trace_id: str, step: int) -> str:
        """Read one step of a run: its kind, status, attributes, input and output (shortened)."""
        return wrap(get_step, trace_id, step)

    @server.tool(name="suggest_fixes", annotations=read)
    def suggest_fixes_tool(trace_id: str) -> str:
        """What to change to fix a failed run, most likely first, and whether each fix has been tried
        and worked (from replays checked by Tracedown)."""
        return wrap(suggest_fixes, trace_id)

    @server.tool(name="check_replay", annotations=read)
    def check_replay_tool(trace_id: str, replay_id: str) -> str:
        """Did a replay fix the run? Checks that the blamed problem is gone, nothing new broke and the
        replay finished."""
        return wrap(check_replay, trace_id, replay_id)

    @server.tool(name="check_claims", annotations=read)
    def check_claims_tool(trace_id: str) -> str:
        """Check each sentence of the run's final answer against its sources: supported, unsupported,
        contradicted or stale."""
        return wrap(check_claims, trace_id)

    @server.tool(name="list_incidents", annotations=read)
    def list_incidents_tool(status: str = "open", project: Optional[str] = None) -> str:
        """Recurring problems: failed runs grouped by the same root cause, most severe first, with a fix.
        status: open, acknowledged, resolved or all."""
        return wrap(list_incidents, status, project)

    @server.tool(name="diagnose_trace", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                                                    openWorldHint=False))
    def diagnose_trace_tool(export: str) -> str:
        """Diagnose a trace export the user pasted: Langfuse, OpenTelemetry (OTLP/JSON) or a Tracedown
        bundle. Imports it into the "pasted" project and returns the root cause of each run."""
        return wrap(diagnose_trace, export)

    return server


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="tracedown mcp", description="Run Tracedown as an MCP server (stdio).")
    ap.add_argument("--endpoint", default=os.environ.get("TDN_ENDPOINT", DEFAULT_ENDPOINT),
                    help=f"Tracedown server URL (default: $TDN_ENDPOINT or {DEFAULT_ENDPOINT})")
    ap.add_argument("--api-key-env", default="TDN_API_KEY",
                    help="name of the environment variable that holds the API key (default: TDN_API_KEY)")
    args = ap.parse_args(argv)
    try:
        client = Client(args.endpoint, os.environ.get(args.api_key_env))
    except TracedownError as e:
        print(f"tracedown mcp: {e}", file=sys.stderr)
        return 2
    build(client).run("stdio")
    return 0
