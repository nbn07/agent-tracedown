"""Deterministic detectors. Each looks at one run and emits candidate findings.

Detectors are cheap and run on every run. Confidence values are calibrated
heuristics, not probabilities, until the RCA benchmark reports precision/recall.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

CATEGORY_LABELS = {
    "tool_failure": "Tool failure",
    "invalid_tool_args": "Invalid tool arguments",
    "loop": "Agent loop",
    "retrieval_failure": "Retrieval failure",
    "stale_source": "Outdated retrieval source",
    "context_window": "Context window exhausted",
    "hallucination": "Unsupported claim (hallucination)",
    "memory_corruption": "Memory corruption",
    "planning_failure": "Planning failure",
    "bad_llm_output": "Bad LLM output",
    "unhandled_error": "Unhandled error",
    "reported_failure": "Run marked failed",
    "wrong_tool": "Wrong tool selected",
    "unverified_action": "Suspect action (first state change)",
    "judged": "Diagnosis by LLM judge",
    "incomplete_task": "Task left incomplete",
    "policy_violation": "Policy violation",
    "prompt_injection": "Prompt injection",
}


@dataclass
class Finding:
    category: str
    title: str
    span_id: str
    step: int
    confidence: float
    detector: str
    evidence: List[str] = field(default_factory=list)
    symptom: bool = False          # where the failure surfaced, not necessarily why
    caused_by: Optional[str] = None  # finding id of the upstream root cause
    id: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "category": self.category, "label": CATEGORY_LABELS.get(self.category, self.category),
            "title": self.title, "span_id": self.span_id, "step": self.step,
            "confidence": round(self.confidence, 2), "detector": self.detector,
            "evidence": self.evidence, "symptom": self.symptom, "caused_by": self.caused_by,
        }


@dataclass
class RunView:
    """A run with decoded payloads, the shape every detector receives."""
    spans: List[Dict[str, Any]]
    blobs: Dict[str, str]

    def __post_init__(self) -> None:
        self.spans = sorted(self.spans, key=lambda s: s["attributes"].get("tdn.step", 0))
        self.by_id = {s["span_id"]: s for s in self.spans}
        self.root = next((s for s in self.spans if not s["parent_span_id"]), self.spans[0])

    def payload(self, digest: Optional[str]) -> Any:
        if not digest or digest not in self.blobs:
            return None
        try:
            return json.loads(self.blobs[digest])
        except ValueError:
            return self.blobs[digest]

    def input(self, span) -> Any:
        return self.payload(span.get("input_digest"))

    def output(self, span) -> Any:
        return self.payload(span.get("output_digest"))

    def step(self, span) -> int:
        return int(span["attributes"].get("tdn.step", 0))

    def of_kind(self, kind: str) -> List[Dict[str, Any]]:
        return [s for s in self.spans if s["kind"] == kind]


Detector = Callable[[RunView], List[Finding]]
DETECTORS: List[Detector] = []


def detector(fn: Detector) -> Detector:
    DETECTORS.append(fn)
    return fn


def text_of(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    return json.dumps(value, default=str)


# ----------------------------------------------------------------------------- detectors

_READ_VERBS = ("get", "list", "search", "find", "read", "fetch", "lookup", "look", "query", "retrieve",
               "calculate", "compute", "think", "check", "validate", "describe", "show", "count", "view")


def has_side_effects(span: Dict[str, Any], declared: Dict[str, Any]) -> bool:
    """Does this tool change the world? Declared ``side_effects`` wins; otherwise the verb in
    its name decides (``get_order`` reads, ``cancel_order`` writes)."""
    name = span["attributes"].get("gen_ai.tool.name") or span["name"]
    decl = declared.get(name) or {}
    if "side_effects" in decl:
        return bool(decl["side_effects"])
    verb = re.split(r"[_\-.]|(?<=[a-z])(?=[A-Z])", name)[0].lower()
    return verb not in _READ_VERBS


# Errors that mean "the service or network failed", as opposed to "the system said no".
_INFRA = re.compile(r"\b(5\d\d|timed? ?out|timeout|unavailable|connection|refused to connect|reset by peer|"
                    r"internal (server )?error|bad gateway|service error|rate.?limit|429|throttl)", re.I)


@detector
def tool_errors(run: RunView) -> List[Finding]:
    out = []
    declared = {t.get("name"): t for t in (run.root["attributes"].get("tdn.tools.available") or [])
                if isinstance(t, dict)}
    successful_writes = [t for t in run.of_kind("tool") if t["status"]["code"] != "ERROR"
                         and has_side_effects(t, declared)]
    for s in run.of_kind("tool"):
        if s["status"]["code"] != "ERROR":
            continue
        a = s["attributes"]
        tool = a.get("gen_ai.tool.name", s["name"])
        invalid = a.get("tdn.tool.args_valid") is False
        writes = has_side_effects(s, declared)
        later = [t for t in run.spans if run.step(t) > run.step(s) and t["kind"] in ("tool", "llm")
                 and t["status"]["code"] != "ERROR"]
        if not writes and not invalid and later:
            # A failed lookup changes nothing; the agent carried on, so it did not decide the outcome.
            out.append(Finding("tool_failure", f"{tool}() lookup failed and the agent continued: {s['status']['message']}",
                               s["span_id"], run.step(s), 0.3, "tool_errors",
                               [f"{tool} reads data only; {len(later)} later step(s) succeeded"]))
            continue
        if writes and not invalid and not _INFRA.search(s["status"]["message"] or ""):
            # The system refused an action: the agent attempted something whose precondition did not hold.
            decided_elsewhere = [t for t in successful_writes if t is not s]
            out.append(Finding("planning_failure", f"Attempted {tool}() but the system refused: {s['status']['message']}",
                               s["span_id"], run.step(s), 0.5 if decided_elsewhere else 0.85, "tool_errors",
                               [f"step {run.step(s)} {s['status']['message']}",
                                f"arguments: {json.dumps(run.input(s), default=str)[:200]}",
                                (f"{len(decided_elsewhere)} other state-changing call(s) did succeed"
                                 if decided_elsewhere else "no other state-changing call succeeded")]))
            continue
        retry = next((t for t in run.of_kind("tool") if run.step(t) > run.step(s) and t["status"]["code"] != "ERROR"
                      and t["attributes"].get("gen_ai.tool.name") == tool), None)
        if retry is not None:
            # Recovered: the agent called the same tool successfully later (same or corrected
            # arguments), so this error did not decide the outcome.
            same = retry.get("input_digest") == s.get("input_digest")
            out.append(Finding("tool_failure", f"{tool}() failed and the agent recovered: {s['status']['message']}",
                               s["span_id"], run.step(s), 0.3, "tool_errors",
                               [f"step {run.step(retry)} called {tool}() successfully"
                                + (" with the same arguments" if same else " with corrected arguments")]))
            continue
        evidence = [f"step {run.step(s)} status ERROR: {s['status']['message']}",
                    f"arguments: {json.dumps(run.input(s), default=str)[:200]}"]
        answer = text_of(run.output(run.root))
        claim = _SUCCESS_CLAIM.search(answer) if writes and later else None
        if claim and not successful_writes:
            # The silent failure: the action failed, the agent carried on and told the user it worked.
            sentence = next((x for x in re.split(r"(?<=[.!?])\s+", answer) if claim.group(0) in x), answer)
            evidence.append(f"the final answer still reports success: {sentence[:160]!r}")
        out.append(Finding(
            category="invalid_tool_args" if invalid else "tool_failure",
            title=f"{tool}() {'rejected its arguments' if invalid else 'failed'}: {s['status']['message']}",
            span_id=s["span_id"], step=run.step(s), confidence=0.9, detector="tool_errors", symptom=True,
            evidence=evidence,
        ))
    return out


_SUCCESS_CLAIM = re.compile(r"\b(confirmed|has been (placed|sent|booked|processed|cancelled|refunded)|"
                            r"successfully|is on its way|will ship|all set|is done)\b", re.I)


def _schema_errors(args: Any, schema: Optional[Dict[str, Any]]) -> Optional[List[str]]:
    """Same subset as the SDK's validate_args: required keys, types, date-time format."""
    if not isinstance(args, dict) or not schema:
        return None
    errs = [f"missing {k}" for k in schema.get("required", []) if k not in args]
    props = schema.get("properties", {})
    types = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict, "array": list}
    for k, v in args.items():
        spec = props.get(k)
        if spec is None:
            if schema.get("additionalProperties") is False:
                errs.append(f"unexpected {k}")
            continue
        if spec.get("type") in types and not isinstance(v, types[spec["type"]]):
            errs.append(f"{k} type")
        elif spec.get("format") == "date-time" and isinstance(v, str) and not re.match(r"^\d{4}-\d{2}-\d{2}T", v):
            errs.append(f"{k} format")
    return errs


def _words(text: str) -> set:
    return {w.lower().rstrip("s") for w in re.findall(r"[A-Za-z]{3,}", text or "")} - _COMMON


_COMMON = set("the and for you your with this that from have please can want would like get make need "
              "use using tool call function return returns given".split())


@detector
def wrong_tool(run: RunView) -> List[Finding]:
    """The agent called tool X where tool Y fits.

    Needs the tools offered to the model (``tdn.declare_tools`` / framework plugins
    record ``tdn.tools.available``). Two signals:
    1. the arguments fail X's schema but satisfy Y's (strong);
    2. the run failed and the user's request matches Y's description far better than X's (weaker).
    """
    tools = run.root["attributes"].get("tdn.tools.available") or []
    if len(tools) < 2:
        return []
    by_name = {t.get("name"): t for t in tools if t.get("name")}
    request = _words(json.dumps(run.input(run.root), default=str))
    failed = run.root["attributes"].get("tdn.run.outcome") == "failure" or run.root["status"]["code"] == "ERROR"
    out = []
    for s in run.of_kind("tool"):
        name = s["attributes"].get("gen_ai.tool.name")
        chosen = by_name.get(name)
        if chosen is None:
            continue
        args = run.input(s)
        errs = _schema_errors(args, chosen.get("schema"))
        fits = [n for n, t in by_name.items() if n != name and _schema_errors(args, t.get("schema")) == []]
        if errs and fits:
            out.append(Finding("wrong_tool", f"Called {name}() but the arguments fit {fits[0]}()",
                               s["span_id"], run.step(s), 0.75, "wrong_tool",
                               [f"{name} schema rejects the arguments: {', '.join(errs)}",
                                f"{fits[0]} schema accepts the same arguments",
                                f"arguments: {json.dumps(args, default=str)[:160]}"]))
            continue
        if failed and request and has_side_effects(s, by_name):
            def overlap(t):
                return len(request & _words(f"{t.get('name', '')} {t.get('description', '')}".replace("_", " ")))
            mine = overlap(chosen)
            best_name, best = max(((n, overlap(t)) for n, t in by_name.items() if n != name),
                                  key=lambda x: x[1], default=(None, 0))
            if best_name and best >= 2 and best >= 2 * max(mine, 1):
                # Keyword overlap is a hint, not evidence (38% precision on real τ-bench runs).
                out.append(Finding("wrong_tool", f"Request matches {best_name}() better than the chosen {name}()",
                                   s["span_id"], run.step(s), 0.45, "wrong_tool",
                                   [f"request shares {best} terms with {best_name}'s description, {mine} with {name}'s"]))
    return out


@detector
def point_of_no_return(run: RunView) -> List[Finding]:
    """In a run that failed on its outcome, the first state-changing tool call is the prime
    suspect: before it nothing was decided; after it, the damage was done.

    A prior, not proof. It ranks below any direct evidence and above "run marked failed".
    """
    r = run.root
    if r["status"]["code"] != "ERROR" and r["attributes"].get("tdn.run.outcome") != "failure":
        return []
    declared = {t.get("name"): t for t in (r["attributes"].get("tdn.tools.available") or []) if isinstance(t, dict)}
    writes = [s for s in run.of_kind("tool") if has_side_effects(s, declared)]
    if not writes:
        return []
    # The first *attempted* state change, even if the system refused it: that is where the
    # agent first committed to a course (measured better than "first successful" on τ-bench dev).
    first = writes[0]
    name = first["attributes"].get("gen_ai.tool.name", first["name"])
    others = len(writes) - 1
    return [Finding("unverified_action", f"First state-changing call before the failure: {name}()",
                    first["span_id"], run.step(first), 0.55, "point_of_no_return",
                    [f"arguments: {json.dumps(run.input(first), default=str)[:200]}",
                     f"{others} later state-changing call(s)" if others else "the only state-changing call in the run",
                     "no direct error explains the failure; check this action against the request"])]


@detector
def loops(run: RunView) -> List[Finding]:
    out = []
    groups: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for s in run.of_kind("tool"):
        groups[(s["attributes"].get("gen_ai.tool.name", s["name"]), s.get("input_digest"))].append(s)
    for (tool, _), spans in groups.items():
        if len(spans) >= 3:
            steps = [run.step(s) for s in spans]
            out.append(Finding(
                category="loop", title=f"{tool}() called {len(spans)} times with identical arguments",
                span_id=spans[0]["span_id"], step=steps[0], confidence=0.9, detector="loops",
                evidence=[f"steps {steps} share one argument digest",
                          f"arguments: {json.dumps(run.input(spans[0]), default=str)[:160]}"],
            ))
    budget = run.root["attributes"].get("tdn.step_budget")
    if budget and len(run.spans) - 1 > int(budget):
        out.append(Finding(
            category="loop", title=f"Run used {len(run.spans) - 1} steps, over its budget of {budget}",
            span_id=run.root["span_id"], step=0, confidence=0.8, detector="loops",
            evidence=[f"tdn.step_budget={budget}"]))
    return out


@detector
def retrieval_quality(run: RunView) -> List[Finding]:
    out = []
    for s in run.of_kind("retrieval"):
        a = s["attributes"]
        chunks = a.get("tdn.retrieval.chunks") or []
        q = a.get("tdn.retrieval.query", "")
        stale = [c for c in chunks if c.get("stale")]
        if a.get("tdn.retrieval.count", len(chunks)) == 0:
            out.append(Finding("retrieval_failure", f"Retrieval returned no chunks for {q!r}", s["span_id"],
                               run.step(s), 0.75, "retrieval_quality", [f"query: {q!r}", "0 chunks"]))
        elif stale:
            srcs = ", ".join(f"{c.get('source') or c.get('id')} (v{c.get('version', '?')})" for c in stale)
            out.append(Finding("stale_source", f"Retrieved an outdated source: {srcs}", s["span_id"],
                               run.step(s), 0.8, "retrieval_quality",
                               [f"query: {q!r}", f"{len(stale)} of {len(chunks)} chunks marked stale/superseded"]))
        elif isinstance(a.get("tdn.retrieval.max_score"), (int, float)) and a["tdn.retrieval.max_score"] < 0.5:
            out.append(Finding("retrieval_failure",
                               f"Low-relevance retrieval for {q!r} (best score {a['tdn.retrieval.max_score']:.2f})",
                               s["span_id"], run.step(s), 0.7, "retrieval_quality",
                               [f"max score {a['tdn.retrieval.max_score']:.2f} < 0.50"]))
    return out


# Instructions aimed at the model, hidden in content it reads (a web page, a document, a tool result).
_INJECTION = re.compile(
    r"(ignore (all |any |the )?(previous|prior|above|earlier) (instructions|prompts?|rules)|"
    r"disregard (all |any |the |your )?(previous|prior|above|earlier) (instructions|prompts?|rules)|"
    r"forget (all |your )?(previous|prior) instructions|new instructions\s*:|"
    r"important\s*:\s*new task\b|"
    r"assistant\s*[,;:]?\s+you\s+(must|need to|are required to)\s+(now\s+)?|"
    r"as an ai model\s*,?\s+you are required to\b|"
    r"ignorez\s+(toutes?\s+)?les instructions (précédentes|precedentes)|"
    r"ignoriere\s+(alle\s+)?vorherigen anweisungen|"
    r"system (note|override|instruction)s?\s*:|do not (tell|inform|mention (this )?to) the user|"
    r"reveal (your|the) (system prompt|instructions)|you are now (in )?(developer|admin|dan)\b)", re.I)
_TARGET = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|https?://[^\s\"'<>)]+")


@detector
def prompt_injection(run: RunView) -> List[Finding]:
    """Content the agent read (retrieved text or a tool result) that tells the model what to do.

    On its own it is a warning. When a later tool call uses an address or link that appears
    in that content and nowhere in the user's request, the agent most likely followed it.
    """
    request = text_of(run.input(run.root))
    out = []
    for s in run.spans:
        if s["kind"] not in ("retrieval", "tool") or s["status"]["code"] == "ERROR":
            continue
        value = run.output(s)
        if isinstance(value, list) and all(isinstance(c, dict) and "text" in c for c in value):
            text = "\n".join(text_of(c["text"]) for c in value)  # retrieved chunks: their text only
        else:
            text = text_of(value)
        m = _INJECTION.search(text)
        if not m:
            continue
        snippet = text[max(0, m.start() - 20): m.end() + 100].strip()
        targets = {t.rstrip(".,;") for t in _TARGET.findall(text[m.start(): m.end() + 400])} - {""}
        targets = {t for t in targets if t not in request}
        used = []
        for t in run.of_kind("tool"):
            if run.step(t) <= run.step(s):
                continue
            args = text_of(run.input(t))
            hit = next((x for x in sorted(targets) if x in args), None)
            if hit:
                used.append((t, hit))
        where = s["attributes"].get("tdn.retrieval.source") or s["attributes"].get("gen_ai.tool.name") or s["name"]
        evidence = [f"step {run.step(s)} content contains instructions for the model: \"…{snippet[:160]}…\""]
        if used:
            t, hit = used[0]
            tool = t["attributes"].get("gen_ai.tool.name", t["name"])
            evidence.append(f"step {run.step(t)} {tool}() used {hit!r}, which appears in that content "
                            f"and not in the user's request")
        out.append(Finding("prompt_injection",
                           f"Instructions hidden in content from {where} at step {run.step(s)}"
                           + (f" were followed at step {run.step(used[0][0])}" if used else ""),
                           s["span_id"], run.step(s), 0.9 if used else 0.6, "prompt_injection", evidence))
    return out


@detector
def context_window(run: RunView) -> List[Finding]:
    out = []
    for s in run.of_kind("llm"):
        u = s["attributes"].get("tdn.llm.context_utilization")
        if isinstance(u, (int, float)) and u >= 0.95:
            out.append(Finding("context_window", f"Prompt filled {u:.0%} of the model's context window",
                               s["span_id"], run.step(s), 0.85, "context_window",
                               [f"input tokens {s['attributes'].get('gen_ai.usage.input_tokens')} of "
                                f"{s['attributes'].get('tdn.llm.context_limit')}"]))
    return out


@detector
def memory_consistency(run: RunView) -> List[Finding]:
    out, last_write = [], {}
    for s in run.of_kind("memory"):
        a = s["attributes"]
        key = a.get("tdn.memory.key")
        if a.get("tdn.memory.op") == "write":
            last_write[key] = (a.get("tdn.memory.value_digest"), run.step(s))
        elif a.get("tdn.memory.op") == "read" and key in last_write:
            written, wstep = last_write[key]
            if written != a.get("tdn.memory.value_digest"):
                out.append(Finding("memory_corruption",
                                   f"Memory key {key!r} read back a different value than step {wstep} wrote",
                                   s["span_id"], run.step(s), 0.8, "memory_consistency",
                                   [f"written {written[:19]}…", f"read {str(a.get('tdn.memory.value_digest'))[:19]}…"]))
    return out


@detector
def planning(run: RunView) -> List[Finding]:
    out = []
    for s in run.of_kind("decision"):
        a = s["attributes"]
        if a.get("tdn.decision.precondition_ok") is False:
            out.append(Finding("planning_failure",
                               f"Chose {a.get('tdn.decision.chosen')!r} although its precondition failed",
                               s["span_id"], run.step(s), 0.65, "planning",
                               [f"options: {a.get('tdn.decision.options')}",
                                f"rationale: {a.get('tdn.decision.rationale') or '(none)'}"]))
    return out


@detector
def hallucination(run: RunView) -> List[Finding]:
    """Claim-level check via the hallucination investigator (``tracedown_server.claims``).

    One finding per run, placed on the LLM step where the first bad claim originated.
    """
    if run.output(run.root) is None:
        return []
    from ..claims import investigate
    report = investigate(run)
    bad = [c for c in report["claims"] if c["label"] in ("contradicted", "unsupported")]
    if not bad or report["summary"]["evidence_units"] <= 1:
        return []  # nothing to check against but the user's own message
    first = min(bad, key=lambda c: (c["origin"] or {"step": 10 ** 9})["step"])
    span_id = (first["origin"] or {}).get("span_id")
    origin = run.by_id.get(span_id) if span_id else None
    if origin is None:
        llm = run.of_kind("llm")
        origin = llm[-1] if llm else run.root
    contradicted = any(c["label"] == "contradicted" for c in bad)
    numeric = any("number" in c["rationale"] for c in bad)
    ev = [f"{c['label']}: {c['text']!r} ({c['rationale']})" for c in bad[:3]]
    return [Finding("hallucination",
                    f"{len(bad)} of {report['summary']['claims']} answer claims not supported by any source",
                    origin["span_id"], run.step(origin), 0.8 if contradicted else 0.75 if numeric else 0.6,
                    "hallucination", ev)]


@detector
def run_status(run: RunView) -> List[Finding]:
    r = run.root
    if r["status"]["code"] != "ERROR" and r["attributes"].get("tdn.run.outcome") != "failure":
        return []
    msg = r["status"]["message"] or "run marked as failure"
    other_errors = [s for s in run.spans if s is not r and s["status"]["code"] == "ERROR"]
    if other_errors:
        return []  # the failing step itself is the better symptom
    cat = "unhandled_error" if re.match(r"^\w+(Error|Exception)\b", msg) else "reported_failure"
    return [Finding(cat, msg, r["span_id"], 0, 0.5, "run_status", [f"root status: {msg}"], symptom=True)]
