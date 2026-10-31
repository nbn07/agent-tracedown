"""Run/step tracing with contextvars propagation, plus replay interception.

Usage::

    import agent_tracedown as tdn
    tdn.init(project="demo", endpoint="http://localhost:7070")
    with tdn.run("CalendarAssistant", input=msg) as run:
        with tdn.agent("PlannerAgent"):
            date = tdn.llm_call(extract, model="gpt-4o-mini", prompt=p)
            tdn.tool_call("calendar_create_event", {"start": date}, create_event)
        run.output("Booked.")
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional

from .exporters import ConsoleExporter, Exporter, HttpExporter
from .spans import Span, new_trace_id

SDK_VERSION = "0.3.0"


# --------------------------------------------------------------------------- config

@dataclass
class Config:
    project: str = "default"
    exporter: Optional[Exporter] = None
    capture_content: bool = True
    redact: Optional[Callable[[Any], Any]] = None
    stream: bool = False


_config = Config(exporter=ConsoleExporter())


def init(project: str = "default", endpoint: Optional[str] = None, api_key: Optional[str] = None,
         exporter: Optional[Exporter] = None, capture_content: bool = True,
         redact: Optional[Callable[[Any], Any]] = None, stream: bool = False) -> Config:
    """Configure the SDK. ``endpoint`` sends to an TDN server; ``exporter`` overrides it.

    ``stream=True`` exports each step as it finishes (live view) instead of the whole
    run at the end; the root span still goes last.
    """
    global _config
    if exporter is None:
        exporter = HttpExporter(endpoint, api_key=api_key, project=project) if endpoint else ConsoleExporter()
    _config = Config(project=project, exporter=exporter, capture_content=capture_content, redact=redact,
                     stream=stream)
    return _config


# --------------------------------------------------------------------------- run state

class ReplayDivergence(RuntimeError):
    """Raised when a replayed run stops matching the recording before the live point."""


@dataclass
class _Replay:
    source_trace_id: str
    mode: str  # exact | from-step | swap | full
    from_step: Optional[int]
    overrides: Dict[str, Any]
    steps: Dict[int, Dict[str, Any]]  # step -> recorded span
    blobs: Dict[str, str]
    served: List[int] = field(default_factory=list)
    live: List[int] = field(default_factory=list)
    first_divergence: Optional[int] = None

    def recorded_for(self, step: int, kind: str) -> Optional[Dict[str, Any]]:
        if self.mode == "full":
            return None
        rec = self.steps.get(step)
        must_record = self.mode == "exact" or (self.mode == "from-step" and step < (self.from_step or 0))
        if rec is None or rec["kind"] != kind:
            if must_record:
                # The agent did something the recording does not have at this step. Running it
                # live would execute real side effects in a replay that promised none.
                found = f"a {rec['kind']} step ({rec['name']})" if rec else "nothing"
                raise ReplayDivergence(f"step {step}: the agent made a {kind} call where the recording has {found}")
            return None
        if self.mode == "exact":
            return rec
        if self.mode == "from-step":
            return rec if step < (self.from_step or 0) else None
        if self.mode == "swap":
            return None if kind == "llm" else rec
        return None


@dataclass
class _RunState:
    trace_id: str
    spans: List[Span] = field(default_factory=list)
    blobs: Dict[str, str] = field(default_factory=dict)
    step: int = 0
    replay: Optional[_Replay] = None
    exported: set = field(default_factory=set)
    exported_blobs: set = field(default_factory=set)

    def flush(self, spans: List[Span]) -> None:
        """Export spans not yet sent, with the blobs not yet sent."""
        if _config.exporter is None:
            return
        todo = [sp for sp in spans if sp.span_id not in self.exported]
        if not todo:
            return
        blobs = {d: c for d, c in self.blobs.items() if d not in self.exported_blobs}
        _config.exporter.export([sp.to_dict() for sp in todo], blobs)
        self.exported.update(sp.span_id for sp in todo)
        self.exported_blobs.update(blobs)

    def next_step(self) -> int:
        n = self.step
        self.step += 1
        return n


_run: contextvars.ContextVar[Optional[_RunState]] = contextvars.ContextVar("tdn_run", default=None)
_span: contextvars.ContextVar[Optional[Span]] = contextvars.ContextVar("tdn_span", default=None)
_pending_replay: contextvars.ContextVar[Optional[_Replay]] = contextvars.ContextVar("tdn_pending_replay", default=None)


def current_trace_id() -> Optional[str]:
    st = _run.get()
    return st.trace_id if st else None


# --------------------------------------------------------------------------- blobs

def _put_blob(state: _RunState, value: Any) -> str:
    """Store a payload by content digest (JSON-encoded) and return the digest."""
    if _config.redact is not None:
        value = _config.redact(value)
    text = json.dumps(value, sort_keys=True, default=str)
    d = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    if _config.capture_content:
        state.blobs[d] = text
    return d


def _get_blob(blobs: Dict[str, str], d: Optional[str]) -> Any:
    if d is None or d not in blobs:
        raise ReplayDivergence(f"recorded payload {d} is missing; was content capture off?")
    return json.loads(blobs[d])


# --------------------------------------------------------------------------- span helpers

@contextlib.contextmanager
def _span_ctx(name: str, kind: str, **attrs: Any) -> Iterator[Optional[Span]]:
    state = _run.get()
    if state is None:  # not inside tdn.run: trace nothing, never break the agent
        yield None
        return
    parent = _span.get()
    span = Span(trace_id=state.trace_id, name=name, kind=kind,
                parent_span_id=parent.span_id if parent else None)
    span.attributes.update({k: v for k, v in attrs.items() if v is not None})
    span.set("tdn.step", state.next_step())
    agent = _nearest_agent(parent)
    if agent and kind != "agent":
        span.set("tdn.agent.name", agent)
    state.spans.append(span)
    token = _span.set(span)
    try:
        yield span
    except BaseException as exc:
        if span.status_code != "ERROR":
            span.error(f"{type(exc).__name__}: {exc}")
            # The bare message, so a replay can raise exactly what the agent saw the first time.
            span.set("tdn.error.message", str(exc)).set("tdn.error.type", type(exc).__name__)
        raise
    finally:
        span.end()
        _span.reset(token)
        if _config.stream:
            state.flush([span])


def _nearest_agent(span: Optional[Span]) -> Optional[str]:
    state = _run.get()
    by_id = {s.span_id: s for s in state.spans} if state else {}
    while span is not None:
        if span.kind == "agent":
            return span.attributes.get("gen_ai.agent.name", span.name)
        span = by_id.get(span.parent_span_id) if span.parent_span_id else None
    return None


class RunHandle:
    def __init__(self, state: _RunState, root: Span):
        self._state, self._root = state, root

    @property
    def trace_id(self) -> str:
        return self._state.trace_id

    def output(self, value: Any) -> None:
        """Record the final response of the run."""
        self._root.output_digest = _put_blob(self._state, value)

    def fail(self, reason: str) -> None:
        """Mark the run failed without raising (e.g. user reported a wrong answer)."""
        self._root.error(reason)
        self._root.set("tdn.run.outcome", "failure")

    def tag(self, **tags: Any) -> None:
        for k, v in tags.items():
            self._root.set(f"tdn.tag.{k}", v)

    def feedback(self, score: float, comment: Optional[str] = None) -> None:
        """User feedback for this run, e.g. 1 = thumbs up, 0 = thumbs down. Later feedback:
        ``POST /api/v1/runs/{trace_id}/feedback``."""
        self._root.set("tdn.feedback.score", score)
        if comment:
            self._root.set("tdn.feedback.comment", comment)

    def tools(self, tools: List[Dict[str, Any]]) -> None:
        """Declare the tools offered to the model: [{"name", "description", "schema"}]."""
        declare_tools(tools)


@contextlib.contextmanager
def run(name: str, input: Any = None, **attrs: Any) -> Iterator[RunHandle]:
    """One end-to-end agent run = one trace."""
    replay = _pending_replay.get()
    state = _RunState(trace_id=new_trace_id(), replay=replay)
    token = _run.set(state)
    span_token = _span.set(None)
    root = Span(trace_id=state.trace_id, name=f"invoke_agent {name}", kind="agent")
    root.set("gen_ai.agent.name", name).set("tdn.run.root", True).set("tdn.step", state.next_step())
    root.set("tdn.sdk.version", SDK_VERSION).set("tdn.project", _config.project)
    for k, v in attrs.items():
        root.set(k if "." in k else f"tdn.{k}", v)
    if replay:
        root.set("tdn.replay.of", replay.source_trace_id).set("tdn.replay.mode", replay.mode)
    if input is not None:
        root.input_digest = _put_blob(state, input)
    state.spans.append(root)
    inner = _span.set(root)
    handle = RunHandle(state, root)
    try:
        yield handle
        root.attributes.setdefault("tdn.run.outcome", "failure" if root.status_code == "ERROR" else "success")
    except BaseException as exc:
        root.error(f"{type(exc).__name__}: {exc}")
        root.set("tdn.run.outcome", "failure")
        raise
    finally:
        root.end()
        _span.reset(inner)
        _span.reset(span_token)
        _run.reset(token)
        if replay:
            root.set("tdn.replay.recorded_steps", list(replay.served))
            root.set("tdn.replay.live_steps", list(replay.live))
            root.set("tdn.replay.first_divergence", replay.first_divergence)
            # The same _Replay may drive several runs in a test; reset its bookkeeping.
            replay.served, replay.live, replay.first_divergence = [], [], None
        state.flush([s for s in state.spans if s is not root] + [root])

def traced_run(name: str, **attrs: Any) -> Callable:
    """Decorate a function so each call is recorded as a run.

    The first positional argument (or, when called by keyword, the first keyword
    argument) is recorded as the run input. The returned value is recorded as
    the run output. Exceptions are recorded by :func:`run` and re-raised.
    """
    def decorate(func: Callable) -> Callable:
        def get_input(args: tuple, kwargs: Dict[str, Any]) -> Any:
            if args:
                return args[0]
            return next(iter(kwargs.values()), None)

        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with run(name, input=get_input(args, kwargs), **attrs) as handle:
                    result = await func(*args, **kwargs)
                    handle.output(result)
                    return result
            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with run(name, input=get_input(args, kwargs), **attrs) as handle:
                result = func(*args, **kwargs)
                handle.output(result)
                return result
        return sync_wrapper
    return decorate




@contextlib.contextmanager
def agent(name: str, role: Optional[str] = None, graph_node: Optional[str] = None) -> Iterator[Optional[Span]]:
    """A (sub-)agent inside the run, e.g. PlannerAgent, ResearchAgent."""
    with _span_ctx(f"invoke_agent {name}", "agent", **{
        "gen_ai.agent.name": name, "tdn.agent.role": role, "tdn.agent.graph_node": graph_node,
    }) as s:
        yield s


# --------------------------------------------------------------------------- step primitives

def declare_tools(tools: List[Dict[str, Any]]) -> None:
    """Record the tools available to the model in this run (enables the wrong-tool check)."""
    state = _run.get()
    if state is None:
        return
    root = next(sp for sp in state.spans if sp.parent_span_id is None)
    root.set("tdn.tools.available", [{"name": t.get("name"), "description": (t.get("description") or "")[:500],
                                      "schema": t.get("schema") or t.get("parameters") or t.get("input_schema")}
                                     for t in tools])


def _replayable(span: Optional[Span], kind: str, input_value: Any) -> Optional[Any]:
    """If replay serves this step from the recording, return ``(value,)``; else None."""
    state = _run.get()
    if span is None or state is None or state.replay is None:
        return None
    rp = state.replay
    step = span.attributes["tdn.step"]
    rec = rp.recorded_for(step, kind)
    if rec is None:
        rp.live.append(step)
        span.set("tdn.replay.source", "live")
        return None
    if rec.get("input_digest") and rec["input_digest"] != span.input_digest:
        if rp.mode == "exact" or (kind == "llm" and not rp.overrides):
            # Nothing was meant to change, so the agent code itself changed.
            raise ReplayDivergence(
                f"step {step} ({span.name}) input differs from the recording; "
                "the agent changed before the replay point")
        # A live step upstream changed this step's input: the recording no longer
        # applies, so run it live and note where the runs diverged.
        if rp.first_divergence is None:
            rp.first_divergence = step
        rp.live.append(step)
        span.set("tdn.replay.source", "live-diverged")
        return None
    rp.served.append(step)
    span.set("tdn.replay.source", "recorded")
    # A step that failed has no output; the caller replays its recorded error.
    if rec.get("output_digest") is None and rec.get("status", {}).get("code") == "ERROR":
        return (None,)
    return (_get_blob(rp.blobs, rec.get("output_digest")),)


def _record_tool_error(rec_span: Dict[str, Any]) -> Optional[str]:
    st = rec_span.get("status", {})
    return st.get("message") if st.get("code") == "ERROR" else None


def llm_call(fn: Callable[..., Any], *, prompt: Any, model: str, provider: str = "custom",
             temperature: Optional[float] = None, seed: Optional[int] = None,
             context_limit: Optional[int] = None, prompt_version: Optional[str] = None,
             cost_usd: Optional[float] = None, **params: Any) -> Any:
    """Call a model. ``fn(prompt, model=..., temperature=..., **params)`` returns text,
    or a dict with ``text`` and optional ``usage`` {input_tokens, output_tokens}."""
    state = _run.get()
    rp = state.replay if state else None
    only = rp.overrides.get("steps") if rp and rp.overrides else None
    if rp and rp.overrides and (only is None or state.step in only):
        model = rp.overrides.get("model", model)
        temperature = rp.overrides.get("temperature", temperature)
        prompt = rp.overrides.get("prompt", prompt)
        fn = rp.overrides.get("llm_fn", fn)
    with _span_ctx(f"chat {model}", "llm", **{
        "gen_ai.request.model": model, "gen_ai.provider.name": provider,
        "gen_ai.request.temperature": temperature, "gen_ai.request.seed": seed,
        "tdn.llm.context_limit": context_limit, "tdn.prompt.version": prompt_version,
    }) as span:
        if span is not None:
            span.input_digest = _put_blob(state, prompt)
        served = _replayable(span, "llm", prompt)
        result = served[0] if served else fn(prompt, model=model, temperature=temperature, **params)
        if span is not None:
            span.output_digest = _put_blob(state, result)
            if isinstance(result, dict) and isinstance(result.get("usage"), dict):
                u = result["usage"]
                span.set("gen_ai.usage.input_tokens", u.get("input_tokens"))
                span.set("gen_ai.usage.output_tokens", u.get("output_tokens"))
                if context_limit and u.get("input_tokens"):
                    span.set("tdn.llm.context_utilization", round(u["input_tokens"] / context_limit, 4))
            cost = cost_usd if cost_usd is not None else (result.get("cost_usd") if isinstance(result, dict) else None)
            if cost is not None:
                span.set("tdn.llm.cost_usd", float(cost))
        return result


def validate_args(args: Dict[str, Any], schema: Optional[Dict[str, Any]]) -> List[str]:
    """Tiny JSON-schema subset: required, type, pattern, format=date-time."""
    if not schema:
        return []
    errors = []
    props = schema.get("properties", {})
    for req in schema.get("required", []):
        if req not in args:
            errors.append(f"missing required argument {req!r}")
    types = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict, "array": list}
    for k, v in args.items():
        spec = props.get(k)
        if spec is None:
            continue
        t = spec.get("type")
        if t in types and not isinstance(v, types[t]):
            errors.append(f"{k} must be {t}, got {type(v).__name__}")
            continue
        if isinstance(v, str):
            if spec.get("format") == "date-time" and not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", v):
                errors.append(f"{k} must be ISO-8601 date-time, got {v!r}")
            if spec.get("pattern") and not re.search(spec["pattern"], v):
                errors.append(f"{k} does not match {spec['pattern']!r}")
    return errors


def tool_call(name: str, args: Dict[str, Any], fn: Callable[..., Any],
              schema: Optional[Dict[str, Any]] = None, raise_on_error: bool = True) -> Any:
    """Execute a tool. Validates ``args`` against ``schema`` and records the result."""
    state = _run.get()
    with _span_ctx(f"execute_tool {name}", "tool", **{"gen_ai.tool.name": name}) as span:
        errors = validate_args(args, schema)
        if span is not None:
            span.input_digest = _put_blob(state, args)
            span.set("tdn.tool.args_valid", not errors)
            if schema:
                span.set("tdn.tool.schema_hash", "sha256:" + hashlib.sha256(
                    json.dumps(schema, sort_keys=True).encode()).hexdigest()[:16])
            if errors:
                span.set("tdn.tool.validation_errors", errors)
        served = _replayable(span, "tool", args)
        if served is not None:
            rec = state.replay.steps[span.attributes["tdn.step"]]
            err = _record_tool_error(rec)
            if err:
                span.error(err)
                message = rec.get("attributes", {}).get("tdn.error.message", err)
                span.set("tdn.error.message", message)
                if raise_on_error:
                    raise ToolError(message)
                return None
            span.output_digest = _put_blob(state, served[0])
            return served[0]
        if errors:
            msg = "; ".join(errors)
            if span is not None:
                span.error(msg)
            if raise_on_error:
                raise ToolError(msg)
            return None
        result = fn(**args)
        if span is not None:
            span.output_digest = _put_blob(state, result)
        return result


class ToolError(RuntimeError):
    pass


def retrieval(query: str, fn: Callable[[str], List[Dict[str, Any]]], source: Optional[str] = None,
              kb_snapshot: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieve chunks. Each chunk: {id, text, source, score, version?, stale?}.
    ``kb_snapshot`` names the index build, so drift reports can compare knowledge-base versions."""
    state = _run.get()
    with _span_ctx("retrieval", "retrieval", **{"tdn.retrieval.query": query, "tdn.retrieval.source": source,
                                                 "tdn.kb.snapshot": kb_snapshot}) as span:
        if span is not None:
            span.input_digest = _put_blob(state, query)
        served = _replayable(span, "retrieval", query)
        chunks = served[0] if served else fn(query)
        if span is not None:
            span.output_digest = _put_blob(state, chunks)
            span.set("tdn.retrieval.count", len(chunks))
            span.set("tdn.retrieval.chunks", [
                {k: c.get(k) for k in ("id", "source", "score", "version", "stale") if k in c} for c in chunks])
            scores = [c.get("score") for c in chunks if isinstance(c.get("score"), (int, float))]
            if scores:
                span.set("tdn.retrieval.max_score", max(scores))
        return chunks


def memory_write(key: str, value: Any) -> None:
    state = _run.get()
    with _span_ctx(f"memory write {key}", "memory", **{"tdn.memory.op": "write", "tdn.memory.key": key}) as span:
        if span is not None:
            span.input_digest = _put_blob(state, value)
            span.set("tdn.memory.value_digest", span.input_digest)


def memory_read(key: str, value: Any) -> Any:
    """Record a read of ``key`` that returned ``value`` (the store itself is yours)."""
    state = _run.get()
    with _span_ctx(f"memory read {key}", "memory", **{"tdn.memory.op": "read", "tdn.memory.key": key}) as span:
        served = _replayable(span, "memory", key)
        if served:
            value = served[0]
        if span is not None:
            span.output_digest = _put_blob(state, value)
            span.set("tdn.memory.value_digest", span.output_digest)
        return value


def decision(options: List[str], chosen: str, rationale: str = "", precondition_ok: bool = True) -> str:
    """Record an explicit choice (a plan step, a routing decision)."""
    state = _run.get()
    with _span_ctx(f"decision {chosen}", "decision", **{
        "tdn.decision.options": options, "tdn.decision.chosen": chosen,
        "tdn.decision.rationale": rationale, "tdn.decision.precondition_ok": precondition_ok,
    }) as span:
        if span is not None:
            span.output_digest = _put_blob(state, chosen)
    return chosen


def handoff(from_agent: str, to_agent: str, payload: Any = None) -> None:
    state = _run.get()
    with _span_ctx(f"handoff {from_agent} -> {to_agent}", "handoff", **{
        "tdn.handoff.from": from_agent, "tdn.handoff.to": to_agent}) as span:
        if span is not None and payload is not None:
            span.input_digest = _put_blob(state, payload)


# --------------------------------------------------------------------------- replay

@contextlib.contextmanager
def replay(bundle: Dict[str, Any], mode: str = "exact", from_step: Optional[int] = None,
           overrides: Optional[Dict[str, Any]] = None) -> Iterator[None]:
    """Replay a recorded run. ``bundle`` = {"spans": [...], "blobs": {...}} for one trace.

    Inside the block, call your agent as usual; the next ``tdn.run`` is replayed.
    Modes: exact (all recorded), from-step (steps < N recorded), swap (LLM steps live
    with ``overrides`` such as model, temperature, prompt or llm_fn), full (all live).
    """
    if mode not in ("exact", "from-step", "swap", "full"):
        raise ValueError(f"unknown replay mode {mode!r}")
    if mode == "from-step" and from_step is None:
        raise ValueError("from-step replay needs from_step")
    spans = bundle["spans"]
    roots = [s for s in spans if s["parent_span_id"] is None]
    rp = _Replay(
        source_trace_id=roots[0]["trace_id"] if roots else spans[0]["trace_id"],
        mode=mode, from_step=from_step, overrides=overrides or {},
        steps={s["attributes"]["tdn.step"]: s for s in spans if "tdn.step" in s["attributes"]},
        blobs=bundle.get("blobs", {}),
    )
    token = _pending_replay.set(rp)
    try:
        yield
    finally:
        _pending_replay.reset(token)


def fetch_bundle(endpoint: str, trace_id: str, api_key: Optional[str] = None) -> Dict[str, Any]:
    """Download a run's spans and blobs from an TDN server for replay."""
    import urllib.request
    req = urllib.request.Request(f"{endpoint.rstrip('/')}/api/v1/runs/{trace_id}/bundle")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


# --------------------------------------------------------------------------- manual traces (framework adapters)

class ManualTrace:
    """A run built from callbacks instead of ``with`` blocks.

    Framework adapters receive start/end events keyed by the framework's own ids
    (LangChain ``run_id``, CrewAI event ids, LlamaIndex span ids), often on other
    threads, so context variables do not apply. Keys map to spans; parents are given
    explicitly. Thread-safe.

        t = ManualTrace("ResearchCrew", input=inputs)
        t.start("agent-1", "Researcher", "agent")
        t.start("tool-7", "execute_tool web_search", "tool", parent="agent-1", input=args,
                attrs={"gen_ai.tool.name": "web_search"})
        t.end("tool-7", output=result)
        t.finish(output=answer)
    """

    def __init__(self, name: str, input: Any = None, **attrs: Any) -> None:
        import threading
        self._lock = threading.RLock()
        self.state = _RunState(trace_id=new_trace_id())
        self.root = Span(trace_id=self.state.trace_id, name=f"invoke_agent {name}", kind="agent")
        self.root.set("gen_ai.agent.name", name).set("tdn.run.root", True).set("tdn.step", self.state.next_step())
        self.root.set("tdn.sdk.version", SDK_VERSION).set("tdn.project", _config.project)
        for k, v in attrs.items():
            self.root.set(k if "." in k else f"tdn.{k}", v)
        if input is not None:
            self.root.input_digest = _put_blob(self.state, input)
        self.state.spans.append(self.root)
        self.spans: Dict[str, Span] = {}
        self.finished = False

    @property
    def trace_id(self) -> str:
        return self.state.trace_id

    def _agent_of(self, span: Optional[Span]) -> Optional[str]:
        by_id = {s.span_id: s for s in self.state.spans}
        while span is not None:
            if span.kind == "agent":
                return span.attributes.get("gen_ai.agent.name", span.name)
            span = by_id.get(span.parent_span_id) if span.parent_span_id else None
        return None

    def start(self, key: str, name: str, kind: str, parent: Optional[str] = None, input: Any = None,
              attrs: Optional[Dict[str, Any]] = None) -> Span:
        with self._lock:
            parent_span = self.spans.get(parent) if parent else None
            parent_span = parent_span or self.root
            span = Span(trace_id=self.trace_id, name=name, kind=kind, parent_span_id=parent_span.span_id)
            span.attributes.update({k: v for k, v in (attrs or {}).items() if v is not None})
            span.set("tdn.step", self.state.next_step())
            if kind == "agent":
                span.attributes.setdefault("gen_ai.agent.name", name.removeprefix("invoke_agent ").strip())
            else:
                agent = self._agent_of(parent_span)
                if agent:
                    span.set("tdn.agent.name", agent)
            if input is not None:
                span.input_digest = _put_blob(self.state, input)
            self.state.spans.append(span)
            self.spans[key] = span
            return span

    def has(self, key: str) -> bool:
        return key in self.spans

    def set(self, key: str, **attrs: Any) -> None:
        with self._lock:
            span = self.spans.get(key)
            if span is not None:
                span.attributes.update({k: v for k, v in attrs.items() if v is not None})

    def end(self, key: str, output: Any = None, error: Optional[str] = None, **attrs: Any) -> Optional[Span]:
        with self._lock:
            span = self.spans.get(key)
            if span is None or span.end_time is not None:
                return span
            span.attributes.update({k: v for k, v in attrs.items() if v is not None})
            if output is not None:
                span.output_digest = _put_blob(self.state, output)
                if span.kind == "retrieval" and isinstance(output, list):
                    span.set("tdn.retrieval.count", len(output))
                    span.set("tdn.retrieval.chunks", [{k: c.get(k) for k in ("id", "source", "score", "version", "stale")
                                                       if k in c} for c in output if isinstance(c, dict)])
                    scores = [c.get("score") for c in output if isinstance(c, dict) and isinstance(c.get("score"), (int, float))]
                    if scores:
                        span.set("tdn.retrieval.max_score", max(scores))
            if error:
                span.error(error)
            span.end()
            if _config.stream:
                self.state.flush([span])
            return span

    def declare_tools(self, tools: List[Dict[str, Any]]) -> None:
        self.root.set("tdn.tools.available", [
            {"name": t.get("name"), "description": (t.get("description") or "")[:500], "schema": t.get("schema"),
             **({"side_effects": bool(t["side_effects"])} if "side_effects" in t else {})}
            for t in tools if t.get("name")])

    def finish(self, output: Any = None, error: Optional[str] = None) -> str:
        with self._lock:
            if self.finished:
                return self.trace_id
            self.finished = True
            for key, span in self.spans.items():  # close anything the framework never ended
                if span.end_time is None:
                    span.set("tdn.unclosed", True)
                    span.end()
            if output is not None:
                self.root.output_digest = _put_blob(self.state, output)
            if error:
                self.root.error(error)
            self.root.attributes.setdefault("tdn.run.outcome", "failure" if self.root.status_code == "ERROR" else "success")
            self.root.end()
            self.state.flush([s for s in self.state.spans if s is not self.root] + [self.root])
            return self.trace_id
