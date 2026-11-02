"""HTTP API.

* ``/v1/*``      ingest (TDN JSON and OTLP/HTTP JSON)
* ``/api/v1/*``  query, analysis, replay, claims, drift, incidents, analytics, admin
* ``/auth/*``    OpenID Connect single sign-on
* ``/metrics``   Prometheus

Every route checks a permission (see ``enterprise.rbac``) against the project the data
belongs to. State-changing calls and payload reads are written to the audit log.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi import Path as PathParam
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import BaseModel, Field, field_validator

from .. import __version__, analytics, drift, incidents, pricing
from ..cache import RateLimiter, cache_from_env, wrap_store
from ..claims import investigate
from ..enterprise import queue_from_env
from ..enterprise.audit import AuditLog
from ..enterprise.pii import PolicyEngine
from ..enterprise.rbac import Directory, Principal
from ..enterprise.sso import OIDC, SSOError
from ..importers import UnknownFormat, import_any, import_otlp, parse_text
from ..incidents import hooks
from ..pipeline import Pipeline
from ..rca import RunView, analyze
from ..replay import MODES, diff, plan
from ..storage import SQLiteStore, TraceOwnershipError
from .views import build_graph, build_tree, merge_graphs

log = logging.getLogger("tracedown")
# The full web app, exported as static files (web/scripts/build-static.mjs) and shipped in the
# wheel. TDN_WEB_DIR points elsewhere; an empty value turns it off.
WEB = Path(os.environ.get("TDN_WEB_DIR", "") or Path(__file__).resolve().parent.parent / "web")
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_HOST_HEADER = re.compile(r"^(?:\[([0-9a-f:.]+)\]|([a-z0-9.-]+))(?::\d{1,5})?$")
# The only writes a read-only server accepts: re-running the analysis of a run, and planning a replay.
_READ_ONLY_ALLOWED = re.compile(r"^/api/v1/runs/[^/]+/(analyze|replay)$")
NO_WEB_APP = ("Tracedown is running, but this copy has no web app built in. The API is at /api/v1 "
              "(documented at /docs).\n\nPackages from PyPI include the web app. In a source checkout, "
              "build it once with:  node web/scripts/build-static.mjs\nor run it separately: "
              "cd web && npm install && npm run dev\n")
READ_ONLY_MESSAGE = ("This is a read-only demo. To diagnose your own runs, install Tracedown: "
                     "pip install agent-tracedown-server && tracedown up")
IMPORT_LIMIT = int(os.environ.get("TDN_IMPORT_LIMIT_KB", "5120")) * 1024  # pasted exports
IMPORT_MAX_RUNS = 50
_API_PREFIXES = ("api/", "v1/", "auth/", "healthz", "metrics", "docs", "redoc", "openapi.json")
_DETAIL_ROUTES = ("runs", "incidents")  # one prerendered page each, at <route>/_/


def _web_file(path: str) -> Optional[Path]:
    """Map a browser path to a file in the exported web app, or None."""
    parts = [p for p in path.split("/") if p not in ("", ".", "..")]
    if len(parts) >= 2 and parts[0] in _DETAIL_ROUTES:
        parts[1] = "_"  # /runs/<any id>/... -> /runs/_/...
    f = WEB.joinpath(*parts) if parts else WEB
    if f.is_dir():
        f = f / "index.html"
    elif not f.exists() and f.with_suffix(".html").exists():
        f = f.with_suffix(".html")
    try:
        f.resolve().relative_to(WEB.resolve())
    except ValueError:
        return None
    return f if f.is_file() else None


def web_available() -> bool:
    return os.environ.get("TDN_WEB_DIR", None) != "" and (WEB / "index.html").is_file()


# ----------------------------------------------------------------------------- request models

INT64 = 2 ** 63 - 1
MAX_SPANS_PER_REQUEST = int(os.environ.get("TDN_MAX_SPANS_PER_REQUEST", "10000"))
MAX_BODY_BYTES = int(float(os.environ.get("TDN_MAX_BODY_MB", "20")) * 1024 * 1024)
DbId = Annotated[int, PathParam(ge=0, le=INT64)]
_INT_ATTRS = ("tdn.step", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens")


class SpanIn(BaseModel):
    trace_id: str = Field(min_length=1, max_length=128)
    span_id: str = Field(min_length=1, max_length=128)
    parent_span_id: Optional[str] = Field(None, max_length=128)
    name: str = Field(max_length=1000)
    kind: str = Field(max_length=32)
    start_time: int = Field(ge=0, le=INT64)
    end_time: Optional[int] = Field(None, ge=0, le=INT64)
    status: Dict[str, Any] = Field(default_factory=lambda: {"code": "UNSET", "message": ""})
    attributes: Dict[str, Any] = Field(default_factory=dict)
    input_digest: Optional[str] = Field(None, max_length=200)
    output_digest: Optional[str] = Field(None, max_length=200)

    @field_validator("attributes")
    @classmethod
    def _numbers_are_numbers(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        for k in _INT_ATTRS:
            x = v.get(k)
            if x is not None and (isinstance(x, bool) or not isinstance(x, int) or not 0 <= x <= INT64):
                raise ValueError(f"{k} must be a whole number between 0 and 2^63-1")
        return v


class SpanBatch(BaseModel):
    project: str = Field("default", min_length=1, max_length=200)
    spans: List[SpanIn] = Field(max_length=MAX_SPANS_PER_REQUEST)


class BlobBatch(BaseModel):
    project: str = "default"
    blobs: Dict[str, str]


class ReplayRequest(BaseModel):
    mode: str = "exact"
    from_step: Optional[int] = None
    overrides: Dict[str, Any] = Field(default_factory=dict)


class Feedback(BaseModel):
    score: float
    comment: Optional[str] = None


class StatusChange(BaseModel):
    status: str


class HookIn(BaseModel):
    kind: str
    config: Dict[str, Any]
    events: Optional[List[str]] = None
    min_severity: str = "medium"
    project: Optional[str] = None
    name: Optional[str] = None


class KeyIn(BaseModel):
    project: str
    role: str = "ingest"
    name: Optional[str] = None


class RolesIn(BaseModel):
    roles: Dict[str, Optional[str]]
    org_role: Optional[str] = None


# ----------------------------------------------------------------------------- app

def create_app(store: Optional[SQLiteStore] = None, api_keys: Optional[List[str]] = None,
               notifier_transport=None, oidc: Optional[OIDC] = None, queue=None,
               require_auth: Optional[bool] = None, cache: Any = "env") -> FastAPI:
    if store is None:
        from ..enterprise import storage_from_env
        path = os.environ.get("TDN_DB", "agent_tracedown.db")
        store = storage_from_env(path) or SQLiteStore(path)
    # TDN_REDIS_URL: shared read cache, live feed across replicas, shared ingest rate limits.
    cache = cache_from_env() if cache == "env" else cache
    store = wrap_store(store, cache)
    store.ensure_schema(analytics.SCHEMA)
    policies = PolicyEngine(store)
    notifier = hooks.Notifier(store, base_url=os.environ.get("TDN_PUBLIC_URL", ""),
                              **({"transport": notifier_transport} if notifier_transport else {}))
    pipeline = Pipeline(store, notifier=notifier, pii=policies, bus=cache.event_bus() if cache else None)
    limiter = RateLimiter(int(os.environ.get("TDN_INGEST_RATE_LIMIT", "0")), cache)

    def cached(name: str, fn, project: Optional[str] = None, args: tuple = (), ttl: int = 30):
        return cache.get_or_set(name, fn, project=project, args=args, ttl=ttl) if cache else fn()

    def rate_limit(p: Principal) -> None:
        if not limiter.allow(p.id):
            raise HTTPException(429, f"ingest rate limit: {limiter.per_minute} requests per minute")
    directory = Directory(store, static_keys=api_keys, require_auth=require_auth)
    audit = AuditLog(store)
    oidc = oidc if oidc is not None else OIDC.from_env()
    # queue=False forces inline processing; None reads TDN_KAFKA_BOOTSTRAP / TDN_QUEUE.
    ingest_queue = None if queue is False else queue if queue is not None else queue_from_env(pipeline.process_spans)

    app = FastAPI(title="Tracedown", version=__version__, description="The flight recorder for AI agents.")

    @app.exception_handler(sqlite3.OperationalError)
    async def sqlite_error(request: Request, exc: sqlite3.OperationalError):
        log.error("database error on %s %s: %s", request.method, request.url.path, exc)
        if "readonly" in str(exc):
            return JSONResponse(status_code=503, content={"detail": (
                "The database file cannot be written. If it is in a OneDrive, iCloud, Dropbox or Google Drive "
                "folder, the sync client may have locked it: restart with a local path, for example "
                "`tracedown up --db ~/.tracedown/tracedown.db`. Otherwise check the file's permissions.")})
        return JSONResponse(status_code=500, content={"detail": f"database error: {exc}"})

    @app.exception_handler(UnicodeEncodeError)
    @app.exception_handler(OverflowError)
    @app.exception_handler(RecursionError)
    async def bad_input(request: Request, exc: Exception):
        reason = {"UnicodeEncodeError": "text that is not valid Unicode (an unpaired surrogate)",
                  "OverflowError": "a number too large to store",
                  "RecursionError": "data nested too deeply"}[type(exc).__name__]
        return JSONResponse(status_code=422, content={"detail": f"The request contains {reason}."})

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        log.exception("unexpected error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={
            "detail": f"Internal error ({type(exc).__name__}: {str(exc)[:200]}). The full error is in the server's log."})
    app.state.store, app.state.pipeline, app.state.directory, app.state.audit = store, pipeline, directory, audit
    app.state.cache = cache

    # -------------------------------------------------------------- auth plumbing
    def principal(request: Request) -> Principal:
        p = directory.resolve(request.headers.get("authorization"), request.cookies.get("tdn_session"))
        if p is None:
            raise HTTPException(401, "authentication required: API key (Bearer tdn_...) or SSO session")
        request.state.principal = p
        return p

    def need(p: Principal, permission: str, project: Optional[str] = None) -> None:
        if not p.can(permission, project):
            raise HTTPException(403, f"missing permission {permission}" + (f" on project {project}" if project else ""))

    def actor(p: Principal) -> str:
        return p.email or p.id

    # Read-only mode, for a public demo: anyone can look and re-run the analysis, nobody can
    # write. `tracedown up --read-only` switches it on after loading the demo runs.
    app.state.read_only = os.environ.get("TDN_READ_ONLY") == "1"

    # While `tracedown up --read-only` loads its demo runs, only the loader (which knows this token)
    # gets in; everyone else waits for read-only mode, so nothing can be written in between.
    app.state.loading_token = None

    @app.middleware("http")
    async def read_only_guard(request: Request, call_next):
        path = request.url.path
        token = app.state.loading_token
        if token is not None and path != "/healthz" \
                and request.headers.get("authorization") != f"Bearer {token}":
            return JSONResponse({"detail": "Tracedown is starting up. Try again in a few seconds."},
                                status_code=503, headers={"Retry-After": "5"})
        if app.state.read_only:
            if path.startswith("/api/v1/admin/"):
                return JSONResponse({"detail": READ_ONLY_MESSAGE}, status_code=403)
            if request.method not in ("GET", "HEAD", "OPTIONS") and not _READ_ONLY_ALLOWED.match(path):
                return JSONResponse({"detail": READ_ONLY_MESSAGE}, status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def audit_mutations(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if app.state.read_only:
            return response  # nothing is written, and refused attempts must not fill the database
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and (path.startswith("/api/") or path.startswith("/auth/")):
            p = getattr(request.state, "principal", None)
            audit.append(actor(p) if p else "unauthenticated", f"{request.method} {path}",
                         ip=request.client.host if request.client else None,
                         outcome="ok" if response.status_code < 400 else f"http_{response.status_code}")
        return response

    @app.middleware("http")
    async def body_limit(request: Request, call_next):
        """Refuse request bodies over TDN_MAX_BODY_MB (default 20 MB); /import has its own 5 MB limit."""
        if request.method in ("POST", "PUT", "PATCH"):
            size = request.headers.get("content-length")
            if size is not None and size.isdigit() and int(size) > MAX_BODY_BYTES:
                return JSONResponse({"detail": f"request body too large: {int(size) // 1024 // 1024} MB, the limit is "
                                               f"{MAX_BODY_BYTES // 1024 // 1024} MB (TDN_MAX_BODY_MB)"},
                                    status_code=413)
            if size is None:  # chunked: count as it arrives
                receive, seen = request._receive, [0]

                async def counted():
                    msg = await receive()
                    seen[0] += len(msg.get("body", b""))
                    if seen[0] > MAX_BODY_BYTES:
                        raise HTTPException(413, "request body too large")
                    return msg
                request._receive = counted
        return await call_next(request)

    # Open mode (no keys, no users) trusts whoever can reach the server. Two checks keep that to
    # the machine itself: a browser page from another site cannot send writes (Origin must match
    # Host), and when the server listens on localhost only, requests must name a local host, which
    # stops DNS rebinding. TDN_ALLOWED_HOSTS adds names (comma-separated); "*" turns the host check off.
    # TDN_BIND_HOST is set by `tracedown up` and `tracedown-server` to the address they listen on.
    app.state.allowed_hosts = LOCAL_HOSTS if os.environ.get("TDN_BIND_HOST", "") in LOCAL_HOSTS else None

    def _hostname(value: str) -> Optional[str]:
        """The host part of a Host header, or None when it is not a plain host[:port]."""
        m = _HOST_HEADER.match(value.strip().lower())
        return (m.group(1) or m.group(2)) if m else None

    @app.middleware("http")
    async def open_mode_guard(request: Request, call_next):
        if not directory.open_mode():
            return await call_next(request)
        host = request.headers.get("host", "")
        allowed = app.state.allowed_hosts
        extra = {h.strip().lower() for h in os.environ.get("TDN_ALLOWED_HOSTS", "").split(",") if h.strip()}
        if allowed is not None and "*" not in extra and _hostname(host) not in (set(allowed) | extra):  # None too
            return JSONResponse({"detail": f"Host {host!r} is not allowed. Open this server as localhost, or add "
                                           "the name to TDN_ALLOWED_HOSTS."}, status_code=421)
        origin = request.headers.get("origin")
        if origin and request.method not in ("GET", "HEAD", "OPTIONS"):
            from urllib.parse import urlparse
            o = urlparse(origin)
            same = (o.netloc or "").lower() == host.lower()
            both_local = (o.hostname or "") in LOCAL_HOSTS and _hostname(host) in LOCAL_HOSTS  # `npm run dev`
            if origin == "null" or not (same or both_local):
                return JSONResponse({"detail": "Cross-site request refused: this server has no login, so it only "
                                               "accepts changes from its own pages."}, status_code=403)
        return await call_next(request)

    def visible(p: Principal, project: Optional[str]) -> bool:
        allowed = p.projects()
        return allowed is None or project in allowed

    def run_project(trace_id: str) -> str:
        owner = store.trace_project(trace_id)
        if owner is None:
            raise HTTPException(404, f"run {trace_id} not found")
        return owner

    def require_scope(p: Principal, project: Optional[str]) -> None:
        """Cross-project aggregates are for unrestricted principals; others must name a project."""
        if project is None and not p.unrestricted:
            raise HTTPException(400, "pass project=<name>; your access is limited to specific projects")

    def spans_for(p: Principal, trace_id: str, permission: str = "runs:read") -> List[Dict[str, Any]]:
        need(p, permission, run_project(trace_id))
        spans = store.get_spans(trace_id)
        if not spans:
            raise HTTPException(404, f"run {trace_id} not found")
        return spans

    def blobs_for(spans: List[Dict[str, Any]]) -> Dict[str, str]:
        if not spans:
            return {}
        project = store.trace_project(spans[0]["trace_id"]) or "default"
        return store.get_blobs([d for s in spans for d in (s["input_digest"], s["output_digest"])], project)

    def analysis(trace_id: str, spans: List[Dict[str, Any]]) -> Dict[str, Any]:
        report = analyze(spans, blobs_for(spans))
        store.put_findings(trace_id, report, time.time_ns())
        return report

    ingest = APIRouter(prefix="/v1", tags=["ingest"])
    api = APIRouter(prefix="/api/v1")

    # -------------------------------------------------------------- ingest
    @ingest.post("/ingest/spans")
    def ingest_spans(batch: SpanBatch, response: Response, p: Principal = Depends(principal)) -> Dict[str, Any]:
        need(p, "ingest", batch.project)
        rate_limit(p)
        spans = [s.model_dump() for s in batch.spans]
        try:
            store.check_ownership(batch.project, spans)
            if ingest_queue is not None:
                response.status_code = 202
                return {"accepted": ingest_queue.publish(batch.project, spans), "queued": True}
            return pipeline.process_spans(batch.project, spans)
        except TraceOwnershipError as exc:
            raise HTTPException(409, str(exc)) from exc

    @ingest.post("/ingest/blobs")
    def ingest_blobs(batch: BlobBatch, p: Principal = Depends(principal)) -> Dict[str, Any]:
        need(p, "ingest", batch.project)
        rate_limit(p)
        try:
            return {"accepted": pipeline.process_blobs(batch.project, batch.blobs)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @ingest.post("/traces")
    def ingest_otlp(body: Dict[str, Any], project: str = "default", p: Principal = Depends(principal)) -> Dict[str, Any]:
        """OTLP/HTTP JSON receiver: any agent emitting OTel GenAI spans works without the TDN SDK."""
        need(p, "ingest", project)
        rate_limit(p)
        try:
            accepted = 0
            for _, spans, blobs in import_otlp([body]):
                if blobs:
                    pipeline.process_blobs(project, blobs)
                accepted += pipeline.process_spans(project, spans)["accepted"]
            return {"accepted": accepted}
        except TraceOwnershipError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise HTTPException(422, f"not a valid OTLP/JSON export: {exc}") from exc

    # -------------------------------------------------------------- runs
    @api.get("/facets", tags=["runs"])
    def facets(project: Optional[str] = None, p: Principal = Depends(principal)) -> Dict[str, Any]:
        """Projects this caller can see, and the agents in them (or in one project), for filter menus."""
        need(p, "runs:read", project)
        rows = [r for r in store.query("SELECT DISTINCT project, agent_name FROM runs") if visible(p, r["project"])]
        return {"projects": sorted({r["project"] for r in rows if r["project"]}),
                "agents": sorted({r["agent_name"] for r in rows
                                  if r["agent_name"] and (project is None or r["project"] == project)})}

    @api.get("/runs", tags=["runs"])
    def list_runs(project: Optional[str] = None, agent: Optional[str] = None, outcome: Optional[str] = None,
                  limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0, le=INT64), p: Principal = Depends(principal)) -> Dict[str, Any]:
        need(p, "runs:read", project)
        runs = [r for r in store.list_runs(project, agent, outcome, limit, offset) if visible(p, r["project"])]
        prices = analytics.get_prices(store)
        for r in runs:
            r.update(pricing.run_cost_from_profile(r.get("profile"), prices, r.get("started_at")))
            f = store.get_findings(r["trace_id"])
            r["root_cause"] = f["root_cause"] if f else None
            r["fault"] = f.get("fault") if f else None
            claims = ((r.get("profile") or {}).get("features") or {}).get("claims")
            r["hallucination_score"] = claims["hallucination_score"] if claims else None
            r.pop("attributes", None)
            r.pop("profile", None)
        return {"runs": runs}

    @api.get("/runs/{trace_id}", tags=["runs"])
    def get_run(trace_id: str, p: Principal = Depends(principal)) -> Dict[str, Any]:
        spans = spans_for(p, trace_id)
        run = store.get_run(trace_id)
        prices = analytics.get_prices(store)
        for s in spans:
            if s["kind"] == "llm":
                s["cost_usd"], s["cost_source"] = pricing.span_cost(s, prices)
        if run is not None:
            run = {**run, **pricing.run_cost_from_spans(spans, prices)}
        return {"run": run, "spans": spans, "tree": build_tree(spans), "findings": store.get_findings(trace_id)}

    @api.get("/runs/{trace_id}/bundle", tags=["replay"])
    def get_bundle(trace_id: str, request: Request, p: Principal = Depends(principal)) -> Dict[str, Any]:
        """Everything the SDK needs to replay the run (includes payload content)."""
        spans = spans_for(p, trace_id, "payloads:read")
        if not app.state.read_only:  # a public demo must not grow its database on every read
            audit.append(actor(p), "read payloads (bundle)", trace_id, run_project(trace_id),
                         request.client.host if request.client else None)
        return {"spans": spans, "blobs": blobs_for(spans)}

    @api.get("/runs/{trace_id}/fix-check", tags=["replay"])
    def fix_check(trace_id: str, replay: str, user_test: Optional[bool] = None,
                  p: Principal = Depends(principal)) -> Dict[str, Any]:
        """Did the replay ``replay`` fix run ``trace_id``? See tracedown_server/fixes.py."""
        from .. import fixes
        original = analysis(trace_id, spans_for(p, trace_id))
        replay_spans = spans_for(p, replay)
        rep = analysis(replay, replay_spans)
        root = next((s for s in replay_spans if not s["parent_span_id"]), replay_spans[0])
        if root["attributes"].get("tdn.replay.of") not in (None, trace_id):
            raise HTTPException(400, f"run {replay} is a replay of another run")
        return {"original": trace_id, "replay": replay, **fixes.check(original, rep, user_test)}

    @api.get("/runs/{trace_id}/suggestions", tags=["replay"])
    def suggestions(trace_id: str, p: Principal = Depends(principal)) -> Dict[str, Any]:
        """Suggested fixes, and for those Tracedown can try itself, the replay recipe."""
        from .. import fixes
        spans = spans_for(p, trace_id)
        report = store.get_findings(trace_id) or analysis(trace_id, spans)
        replays = []
        for row in store.query("SELECT trace_id FROM runs WHERE replay_of = ? ORDER BY started_at DESC LIMIT 30",
                               [trace_id]):
            r_spans = store.get_spans(row["trace_id"])
            if not r_spans:
                continue
            root = next((s for s in r_spans if not s["parent_span_id"]), r_spans[0])
            r_report = store.get_findings(row["trace_id"]) or analysis(row["trace_id"], r_spans)
            replays.append({"trace_id": row["trace_id"], "suggestion": root["attributes"].get("tdn.replay.suggestion"),
                            "check": fixes.check(report, r_report)})
        return {"trace_id": trace_id,
                **fixes.with_results(fixes.suggest(report, spans, blobs_for(spans)), replays)}

    @api.get("/runs/{trace_id}/graph", tags=["visualization"])
    def get_graph(trace_id: str, p: Principal = Depends(principal)) -> Dict[str, Any]:
        return build_graph(spans_for(p, trace_id))

    @api.post("/import", tags=["ingest"])
    async def import_export(request: Request, project: str = "pasted", format: str = "auto",
                            p: Principal = Depends(principal)) -> Dict[str, Any]:
        """Diagnose a pasted or uploaded export: Langfuse, OTLP/JSON or a Tracedown bundle."""
        need(p, "ingest", project)
        rate_limit(p)
        raw = await request.body()
        if len(raw) > IMPORT_LIMIT:
            raise HTTPException(413, f"export too large: {len(raw) // 1024} KB, the limit is {IMPORT_LIMIT // 1024} KB")
        try:
            fmt, runs = import_any(parse_text(raw.decode("utf-8", errors="replace")), format)
        except (UnknownFormat, KeyError, TypeError, IndexError, AttributeError, RecursionError) as exc:
            raise HTTPException(422, f"could not read this export: {str(exc)[:300]}") from exc
        if not runs:
            raise HTTPException(422, "the export holds no runs")
        out = []
        try:
            for trace_id, spans, blobs in runs[:IMPORT_MAX_RUNS]:
                if blobs:
                    pipeline.process_blobs(project, blobs)
                store.check_ownership(project, spans)
                pipeline.process_spans(project, spans)
                run = store.get_run(trace_id) or {}
                found = store.get_findings(trace_id) or {}
                rc = found.get("root_cause") or {}
                out.append({"trace_id": trace_id, "agent": run.get("agent_name"), "outcome": run.get("outcome"),
                            "root_cause": rc.get("label"), "step": rc.get("step")})
        except TraceOwnershipError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"format": fmt, "project": project, "runs": out, "skipped": max(0, len(runs) - IMPORT_MAX_RUNS)}

    @api.post("/runs/{trace_id}/analyze", tags=["analysis"])
    def analyze_run(trace_id: str, p: Principal = Depends(principal)) -> Dict[str, Any]:
        return analysis(trace_id, spans_for(p, trace_id, "analysis:run"))

    @api.get("/runs/{trace_id}/stacktrace", response_class=PlainTextResponse, tags=["analysis"])
    def stacktrace(trace_id: str, p: Principal = Depends(principal)) -> str:
        spans = spans_for(p, trace_id)
        report = store.get_findings(trace_id) or analyze(spans, blobs_for(spans))
        return report["stack_trace"]

    @api.get("/runs/{trace_id}/claims", tags=["analysis"])
    def claims(trace_id: str, p: Principal = Depends(principal)) -> Dict[str, Any]:
        """Hallucination investigator: claims, attribution, labels and heatmap segments."""
        spans = spans_for(p, trace_id, "payloads:read")
        return investigate(RunView(spans, blobs_for(spans)))

    @api.post("/runs/{trace_id}/replay", tags=["replay"])
    def replay_plan(trace_id: str, req: ReplayRequest, p: Principal = Depends(principal)) -> Dict[str, Any]:
        spans = spans_for(p, trace_id, "replay:plan")
        if req.mode not in MODES:
            raise HTTPException(422, f"mode must be one of {MODES}")
        try:
            return plan(spans, blobs_for(spans), req.mode, req.from_step, req.overrides)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/runs/{trace_id}/feedback", tags=["runs"])
    def feedback(trace_id: str, fb: Feedback, p: Principal = Depends(principal)) -> Dict[str, Any]:
        """Late user feedback (thumbs up/down) from the agent's own UI; needs an ingest key."""
        project = run_project(trace_id)
        if not (p.can("ingest", project) or p.can("incidents:write", project)):
            raise HTTPException(403, "missing permission ingest")
        run = store.get_run(trace_id)
        if run is None:
            raise HTTPException(409, "run has not finished yet; send feedback after its root span arrives")
        prof = run.get("profile") or {}
        if prof:
            prof["features"]["feedback"] = fb.score
            store.set_profile(trace_id, prof)
        return {"trace_id": trace_id, "feedback": fb.score}

    @api.get("/diff", tags=["replay"])
    def diff_runs(a: str, b: str, p: Principal = Depends(principal)) -> Dict[str, Any]:
        sa, sb = spans_for(p, a), spans_for(p, b)
        return diff(sa, sb, blobs_for(sa), blobs_for(sb))

    @api.get("/blobs/{digest}", tags=["runs"])
    def get_blob(digest: str, request: Request, trace_id: str = Query(..., description="run the payload belongs to"),
                 p: Principal = Depends(principal)) -> Dict[str, Any]:
        project = run_project(trace_id)
        need(p, "payloads:read", project)
        spans = store.get_spans(trace_id)
        if digest not in {d for sp in spans for d in (sp["input_digest"], sp["output_digest"])}:
            raise HTTPException(404, "blob not found in this run")
        found = store.get_blobs([digest], project)
        if digest not in found:
            raise HTTPException(404, "blob not found")
        if not app.state.read_only:
            audit.append(actor(p), "read payload", digest, project, request.client.host if request.client else None)
        try:
            return {"digest": digest, "content": json.loads(found[digest])}
        except ValueError:
            return {"digest": digest, "content": found[digest]}

    # -------------------------------------------------------------- multi-agent visualization
    @api.get("/agents/{agent}/graph", tags=["visualization"])
    def agent_network(agent: str, project: Optional[str] = None, limit: int = Query(200, ge=1, le=2000),
                      p: Principal = Depends(principal)) -> Dict[str, Any]:
        """The agent network across recent runs: call counts, error rates, handoffs."""
        need(p, "runs:read", project)
        runs = [r for r in store.list_runs(project, agent, None, limit, 0)
                if visible(p, r["project"]) and not r["replay_of"]]
        visible_ids = tuple(r["trace_id"] for r in runs)
        return cached("agent_graph", lambda: merge_graphs([build_graph(store.get_spans(t)) for t in visible_ids],
                                                          runs=len(visible_ids)),
                      project=project, args=(agent, visible_ids))

    @api.get("/live", tags=["visualization"])
    def live(request: Request, project: Optional[str] = None, p: Principal = Depends(principal)):
        """Server-Sent Events: spans as they arrive, finished runs, drift alerts."""
        need(p, "runs:read", project)
        sid = pipeline.bus.subscribe()

        def stream():
            try:
                yield "retry: 3000\n\n"
                while True:
                    events = pipeline.bus.drain(sid, timeout=15)
                    if not events:
                        yield ": keep-alive\n\n"
                    for e in events:
                        ep = e.get("project")
                        if not visible(p, ep) or (project and ep != project):
                            continue  # an event without a project is only shown to unrestricted principals
                        yield f"event: {e['type']}\ndata: {json.dumps(e, default=str)}\n\n"
            finally:
                pipeline.bus.unsubscribe(sid)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # -------------------------------------------------------------- analytics and metrics
    @api.get("/analytics", tags=["analytics"])
    def get_analytics(project: Optional[str] = None, agent: Optional[str] = None, days: int = Query(7, ge=1, le=90),
                      p: Principal = Depends(principal)) -> Dict[str, Any]:
        need(p, "metrics:read", project)
        require_scope(p, project)
        return cached("analytics", lambda: analytics.overview(store, project, agent, days), project=project,
                      args=(agent, days))

    @api.get("/metrics/agents", tags=["analytics"])
    def agent_metrics(project: Optional[str] = None, p: Principal = Depends(principal)) -> Dict[str, Any]:
        need(p, "metrics:read", project)
        require_scope(p, project)
        return store.stats(project)

    @app.get("/metrics", response_class=PlainTextResponse, tags=["analytics"])
    def prometheus(project: Optional[str] = None, p: Principal = Depends(principal)) -> str:
        need(p, "metrics:read", project)
        require_scope(p, project)
        return analytics.prometheus(store, project)

    @api.get("/settings/prices", tags=["analytics"])
    def get_prices(p: Principal = Depends(principal)) -> Dict[str, Any]:
        need(p, "metrics:read")
        return analytics.get_prices(store)

    @api.get("/settings/prices/overrides", tags=["analytics"])
    def get_price_overrides(p: Principal = Depends(principal)) -> Dict[str, Any]:
        """Only the prices saved through the API (not the built-in table or TDN_PRICES)."""
        need(p, "metrics:read")
        return analytics.user_prices(store)

    @api.put("/settings/prices", tags=["analytics"])
    def put_prices(prices: Dict[str, Dict[str, Any]], p: Principal = Depends(principal)) -> Dict[str, Any]:
        need(p, "policies:manage")
        try:
            return analytics.set_prices(store, prices)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    # -------------------------------------------------------------- drift
    @api.get("/drift/values", tags=["drift"])
    def drift_values(agent: str, dimension: str, project: Optional[str] = None,
                     p: Principal = Depends(principal)) -> Dict[str, Any]:
        need(p, "drift:read", project)
        require_scope(p, project)
        if dimension not in drift.DIMENSIONS:
            raise HTTPException(422, f"dimension must be one of {drift.DIMENSIONS}")
        return {"agent": agent, "dimension": dimension, "values": drift.values(store, agent, dimension, project)}

    @api.get("/drift/compare", tags=["drift"])
    def drift_compare(agent: str, dimension: str, a: str, b: str, project: Optional[str] = None,
                      min_runs: int = 20, p: Principal = Depends(principal)) -> Dict[str, Any]:
        need(p, "drift:read", project)
        require_scope(p, project)
        try:
            return drift.compare(drift.cohort(store, agent, dimension, a, project),
                                 drift.cohort(store, agent, dimension, b, project), dimension=dimension,
                                 baseline_value=a, candidate_value=b, agent=agent, min_runs=min_runs)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.get("/drift/reports", tags=["drift"])
    def drift_reports(agent: Optional[str] = None, verdict: Optional[str] = None,
                      p: Principal = Depends(principal)) -> Dict[str, Any]:
        need(p, "drift:read")
        return {"reports": [r for r in drift.list_reports(store, agent, verdict) if visible(p, r["project"])]}

    @api.post("/drift/scan", tags=["drift"])
    def drift_scan(agent: str, project: Optional[str] = None, min_runs: int = 20,
                   p: Principal = Depends(principal)) -> Dict[str, Any]:
        need(p, "analysis:run", project)
        require_scope(p, project)
        return {"reports": drift.scan(store, agent, project, min_runs)}

    # -------------------------------------------------------------- incidents
    @api.get("/incidents", tags=["incidents"])
    def list_incidents(project: Optional[str] = None, status: Optional[str] = None,
                       p: Principal = Depends(principal)) -> Dict[str, Any]:
        need(p, "incidents:read", project)
        return {"incidents": [i for i in incidents.list_incidents(store, project, status) if visible(p, i["project"])]}

    def incident_or_404(p: Principal, incident_id: int, permission: str) -> Dict[str, Any]:
        inc = incidents.get(store, incident_id)
        if inc is None:
            raise HTTPException(404, "incident not found")
        need(p, permission, inc["project"])
        return inc

    @api.get("/incidents/{incident_id}", tags=["incidents"])
    def get_incident(incident_id: DbId, p: Principal = Depends(principal)) -> Dict[str, Any]:
        return incident_or_404(p, incident_id, "incidents:read")

    @api.post("/incidents/{incident_id}/status", tags=["incidents"])
    def incident_status(incident_id: DbId, body: StatusChange, p: Principal = Depends(principal)) -> Dict[str, Any]:
        incident_or_404(p, incident_id, "incidents:write")
        try:
            return incidents.set_status(store, incident_id, body.status, actor(p))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    # -------------------------------------------------------------- hooks
    @api.get("/hooks", tags=["incidents"])
    def list_hooks(project: Optional[str] = None, p: Principal = Depends(principal)) -> Dict[str, Any]:
        need(p, "hooks:read", project)
        mine = [h for h in hooks.list_hooks(store, project)
                if p.unrestricted or (h["project"] is not None and visible(p, h["project"]))]
        ids = {h["id"] for h in mine}
        return {"hooks": mine, "deliveries": [d for d in hooks.deliveries(store, limit=200) if d["hook_id"] in ids][:20]}

    @api.post("/hooks", tags=["incidents"])
    def create_hook(body: HookIn, p: Principal = Depends(principal)) -> Dict[str, Any]:
        need(p, "hooks:manage", body.project)
        try:
            return hooks.create(store, body.kind, body.config, body.events, body.min_severity, body.project, body.name)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.delete("/hooks/{hook_id}", tags=["incidents"])
    def delete_hook(hook_id: DbId, p: Principal = Depends(principal)) -> Dict[str, Any]:
        h = hooks.get(store, hook_id)
        if h is None:
            raise HTTPException(404, "hook not found")
        need(p, "hooks:manage", h["project"])
        hooks.delete(store, hook_id)
        return {"deleted": hook_id}

    @api.post("/hooks/{hook_id}/test", tags=["incidents"])
    def test_hook(hook_id: DbId, p: Principal = Depends(principal)) -> Dict[str, Any]:
        h = hooks.get(store, hook_id)
        if h is None:
            raise HTTPException(404, "hook not found")
        need(p, "hooks:manage", h["project"])
        sample = {"id": 0, "title": "Test incident from Tracedown", "severity": "high", "run_count": 1,
                  "root_cause": "This is a test notification.", "suggested_fix": "No action needed.",
                  "category": "test", "agent": "test-agent", "target": None}
        return {"results": notifier.notify("incident.opened", sample, only_hook=hook_id)}

    # -------------------------------------------------------------- policies (PII)
    @api.get("/policies/pii/{project}", tags=["governance"])
    def get_pii(project: str, p: Principal = Depends(principal)) -> Dict[str, Any]:
        need(p, "runs:read", project)
        return {"project": project, "policy": policies.get(project), "redactions": policies.stats(project)}

    @api.put("/policies/pii/{project}", tags=["governance"])
    def put_pii(project: str, policy: Dict[str, Any], p: Principal = Depends(principal)) -> Dict[str, Any]:
        need(p, "policies:manage", project)
        try:
            return {"project": project, "policy": policies.set(project, policy, actor(p))}
        except (ValueError, Exception) as exc:  # re.error from a bad custom pattern
            raise HTTPException(422, str(exc)) from exc

    # -------------------------------------------------------------- admin: users, keys, audit
    @api.get("/me", tags=["admin"])
    def me(p: Principal = Depends(principal)) -> Dict[str, Any]:
        return {**p.public(), "open_mode": directory.open_mode(), "sso": oidc is not None}

    @api.get("/admin/users", tags=["admin"])
    def users(p: Principal = Depends(principal)) -> Dict[str, Any]:
        need(p, "users:manage")
        return {"users": directory.list_users()}

    @api.put("/admin/users/{user_id}/roles", tags=["admin"])
    def set_roles(user_id: DbId, body: RolesIn, p: Principal = Depends(principal)) -> Dict[str, Any]:
        need(p, "users:manage")
        try:
            for project, role in body.roles.items():
                directory.set_membership(user_id, project, role)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if body.org_role in ("owner", "member"):
            store.execute("UPDATE users SET org_role=? WHERE id=?", (body.org_role, user_id))
        return {"users": [u for u in directory.list_users() if u["id"] == user_id]}

    @api.post("/admin/users", tags=["admin"])
    def invite(body: Dict[str, Any], p: Principal = Depends(principal)) -> Dict[str, Any]:
        """Pre-create a user (they sign in through SSO)."""
        need(p, "users:manage")
        if "@" not in str(body.get("email", "")):
            raise HTTPException(422, "email required")
        if directory.open_mode():
            raise HTTPException(409, "This server has no login yet (open mode). Adding a user here would lock "
                                     "everyone out until SSO is set up. Set up SSO (TDN_OIDC_ISSUER) or start the "
                                     "server with TDN_API_KEYS first.")
        roles = body.get("roles") or {}
        if not isinstance(roles, dict):
            raise HTTPException(422, "roles must be an object of project: role")
        from ..enterprise.rbac import ROLES
        bad = {r for r in roles.values() if r is not None and r not in ROLES}
        if bad or body.get("org_role", "member") not in ("owner", "member"):
            raise HTTPException(422, f"unknown role(s) {sorted(map(str, bad)) or body.get('org_role')}")
        u = directory.upsert_user(body["email"], body.get("name"), body.get("org_role", "member"))
        for project, role in roles.items():
            directory.set_membership(u["id"], project, role)
        return u

    def no_lockout() -> None:
        if directory.open_mode():
            raise HTTPException(409, "This server has no login yet (open mode). Creating the first key or user here "
                                     "would lock everyone else out. Start the server with an owner key instead, "
                                     "e.g. TDN_API_KEYS=<a long secret> (or set up SSO), then create keys with it.")

    @api.get("/admin/keys", tags=["admin"])
    def list_keys(project: Optional[str] = None, p: Principal = Depends(principal)) -> Dict[str, Any]:
        need(p, "keys:manage", project)
        return {"keys": [k for k in directory.list_keys(project) if visible(p, k["project"])]}

    @api.post("/admin/keys", tags=["admin"])
    def create_key(body: KeyIn, p: Principal = Depends(principal)) -> Dict[str, Any]:
        need(p, "keys:manage", body.project)
        no_lockout()
        try:
            return directory.create_key(body.project, body.role, body.name, actor(p))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.delete("/admin/keys/{key_id}", tags=["admin"])
    def revoke_key(key_id: DbId, p: Principal = Depends(principal)) -> Dict[str, Any]:
        rows = store.query("SELECT project FROM api_keys WHERE id=?", (key_id,))
        if not rows:
            raise HTTPException(404, "key not found")
        need(p, "keys:manage", rows[0]["project"])
        directory.revoke_key(key_id)
        return {"revoked": key_id}

    @api.get("/admin/audit", tags=["admin"])
    def audit_log(project: Optional[str] = None, actor_: Optional[str] = Query(None, alias="actor"),
                  limit: int = Query(100, ge=1, le=1000), before_seq: Optional[int] = Query(None, ge=0, le=INT64),
                  p: Principal = Depends(principal)) -> Dict[str, Any]:
        need(p, "audit:read")
        return {"entries": audit.list(project, actor_, limit, before_seq), "integrity": audit.verify()}

    # -------------------------------------------------------------- SSO
    @app.get("/auth/oidc/login", tags=["auth"])
    def oidc_login():
        if oidc is None:
            raise HTTPException(404, "SSO is not configured (set TDN_OIDC_ISSUER)")
        url, state = oidc.login_url()
        resp = RedirectResponse(url, status_code=302)
        # Bind the login to this browser: the callback must come back with the same cookie,
        # so a callback URL started by someone else cannot sign this browser in (login CSRF).
        resp.set_cookie("tdn_oidc_state", state, httponly=True, samesite="lax", max_age=600,
                        secure=os.environ.get("TDN_PUBLIC_URL", "").startswith("https"))
        return resp

    @app.get("/auth/oidc/callback", tags=["auth"])
    def oidc_callback(request: Request, code: str = "", state: str = "", error: Optional[str] = None):
        if oidc is None:
            raise HTTPException(404, "SSO is not configured")
        ip = request.client.host if request.client else None
        if error:
            audit.append("unauthenticated", "sso login", outcome="denied", ip=ip, detail={"error": error})
            raise HTTPException(401, f"identity provider returned {error}")
        if not state or request.cookies.get("tdn_oidc_state") != state:
            audit.append("unauthenticated", "sso login", outcome="denied", ip=ip, detail={"error": "state mismatch"})
            raise HTTPException(401, "login state does not match this browser; start again at /auth/oidc/login")
        try:
            ident = oidc.callback(code, state)
        except SSOError as exc:
            audit.append("unauthenticated", "sso login", outcome="denied", ip=ip, detail={"error": str(exc)})
            raise HTTPException(401, str(exc)) from exc
        org_role = ident["org_role"] if ident["owner_mapped"] else ("owner" if ident["org_role"] == "owner" else None)
        user = directory.upsert_user(ident["email"], ident["name"], org_role, ident["subject"])
        for project in ident["managed_projects"]:  # IdP groups are the source of truth for mapped projects
            directory.set_membership(user["id"], project, ident["roles"].get(project))
        token = directory.sessions.issue(user["id"], user["email"])
        audit.append(user["email"], "sso login", ip=ip, detail={"groups": ident["groups"]})
        resp = RedirectResponse("/", status_code=302)
        resp.delete_cookie("tdn_oidc_state")
        resp.set_cookie("tdn_session", token, httponly=True, samesite="lax",
                        secure=os.environ.get("TDN_PUBLIC_URL", "").startswith("https"),
                        max_age=directory.sessions.ttl)
        return resp

    @app.post("/auth/logout", tags=["auth"])
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie("tdn_session")
        return resp

    @app.get("/healthz", tags=["ops"])
    def healthz() -> Dict[str, Any]:
        return {"status": "ok", "version": __version__, "read_only": bool(app.state.read_only)}

    app.include_router(ingest)
    app.include_router(api)
    if web_available():
        @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
        def web(path: str, request: Request):
            if path.startswith(_API_PREFIXES):
                raise HTTPException(404, "not found")
            if path and not path.endswith("/") and "." not in path.rsplit("/", 1)[-1]:
                query = ("?" + request.url.query) if request.url.query else ""
                # Leading slashes or backslashes would make "//evil.example" an off-site redirect.
                local = "/" + path.lstrip("/\\")
                return RedirectResponse(local + "/" + query, status_code=308)  # the export uses trailing slashes
            f = _web_file(path)
            if f is None:
                missing = WEB / "404.html"
                return FileResponse(missing, status_code=404) if missing.is_file() else \
                    PlainTextResponse("not found", status_code=404)
            # Hashed assets never change; pages must pick up a new build at once.
            cache = "public, max-age=31536000, immutable" if path.startswith("_next/static/") else "no-cache"
            return FileResponse(f, headers={"Cache-Control": cache})
    else:
        @app.get("/", include_in_schema=False)
        def index() -> PlainTextResponse:
            return PlainTextResponse(NO_WEB_APP)

    return app
