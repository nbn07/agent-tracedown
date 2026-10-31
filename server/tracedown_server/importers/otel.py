"""OpenTelemetry (OTLP/JSON) importer.

Reads traces from any agent stack that exports OpenTelemetry, whichever attribute
convention it uses:

* OTel GenAI semantic conventions (``gen_ai.operation.name``, ``gen_ai.input.messages``,
  older ``gen_ai.prompt`` / ``gen_ai.completion`` and their span events);
* OpenInference, used by Arize Phoenix (``openinference.span.kind``, ``input.value``,
  ``llm.input_messages.*``);
* OpenLLMetry / Traceloop (``traceloop.span.kind``, ``traceloop.entity.input``,
  ``gen_ai.prompt.0.content``);
* Langfuse's own OTel attributes (``langfuse.observation.type``, ``.input``, ``.output``);
* Tracedown's own export (``tdn.*``), which round-trips exactly.

Input is an OTLP/JSON export: ``{"resourceSpans": [...]}``, one such object per line
(the OTel Collector file exporter), or a list of them.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .common import Node, build_run, maybe_json

_OP_KIND = {"invoke_agent": "agent", "create_agent": "agent", "chat": "llm", "text_completion": "llm",
            "generate_content": "llm", "completion": "llm", "execute_tool": "tool", "embeddings": "embedding",
            "retrieval": "retrieval", "retrieve": "retrieval"}
_OI_KIND = {"LLM": "llm", "TOOL": "tool", "AGENT": "agent", "RETRIEVER": "retrieval", "EMBEDDING": "embedding",
            "RERANKER": "retrieval", "GUARDRAIL": "decision", "EVALUATOR": None, "CHAIN": None}
_TL_KIND = {"agent": "agent", "tool": "tool", "workflow": None, "task": None}
_LF_KIND = {"generation": "llm", "agent": "agent", "tool": "tool", "retriever": "retrieval", "embedding": "embedding",
            "guardrail": "decision", "span": None, "chain": None, "event": None, "evaluator": None}


def any_value(v: Dict[str, Any]) -> Any:
    """Decode one OTLP AnyValue."""
    if not isinstance(v, dict) or not v:
        return None
    if "stringValue" in v:
        return v["stringValue"]
    if "boolValue" in v:
        return v["boolValue"]
    if "intValue" in v:
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return v["intValue"]
    if "doubleValue" in v:
        return v["doubleValue"]
    if "arrayValue" in v:
        return [any_value(x) for x in (v["arrayValue"] or {}).get("values", [])]
    if "kvlistValue" in v:
        return {kv["key"]: any_value(kv.get("value", {})) for kv in (v["kvlistValue"] or {}).get("values", [])}
    if "bytesValue" in v:
        return v["bytesValue"]
    return next(iter(v.values()), None)


def attrs_of(items: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    return {kv["key"]: any_value(kv.get("value", {})) for kv in items or []}


def norm_id(raw: Optional[str], size: int) -> Optional[str]:
    """OTLP/JSON ids are hex; protobuf-JSON exporters write base64. Return hex."""
    if not raw:
        return None
    if re.fullmatch(r"[0-9a-fA-F]+", raw) and len(raw) == size * 2:
        return raw.lower()
    try:
        b = base64.b64decode(raw, validate=True)
        if len(b) == size:
            return b.hex()
    except (binascii.Error, ValueError):
        pass
    return raw


def _indexed_messages(a: Dict[str, Any], prefix: str, role_key: str, content_key: str) -> Optional[List[Dict[str, Any]]]:
    """Flattened message lists: ``gen_ai.prompt.0.role`` / ``llm.input_messages.0.message.content``."""
    rx = re.compile(re.escape(prefix) + r"\.(\d+)\.(.+)$")
    rows: Dict[int, Dict[str, Any]] = defaultdict(dict)
    for k, v in a.items():
        m = rx.match(k)
        if m:
            rows[int(m.group(1))][m.group(2).removeprefix("message.")] = v
    if not rows:
        return None
    out = []
    for i in sorted(rows):
        r = rows[i]
        msg = {"role": r.get(role_key) or "user", "content": r.get(content_key)}
        calls = sorted({k.split(".")[1] for k in r if k.startswith("tool_calls.") and k.count(".") >= 2})
        if calls and not msg["content"]:
            msg["content"] = "; ".join(f"[calls {r.get(f'tool_calls.{c}.name') or r.get(f'tool_calls.{c}.tool_call.function.name')}"
                                       f"({r.get(f'tool_calls.{c}.arguments') or r.get(f'tool_calls.{c}.tool_call.function.arguments') or ''})]"
                                       for c in calls)
        out.append(msg)
    return out


def _kind(name: str, a: Dict[str, Any]) -> Tuple[Optional[str], bool]:
    """(kind, known). known=False means no convention said anything."""
    if a.get("tdn.kind") in ("agent", "llm", "tool", "retrieval", "embedding", "memory", "decision", "handoff"):
        return a["tdn.kind"], True
    for key, table in (("openinference.span.kind", _OI_KIND), ("langfuse.observation.type", _LF_KIND),
                       ("traceloop.span.kind", _TL_KIND)):
        v = a.get(key)
        if isinstance(v, str):
            k = v if key == "openinference.span.kind" else v.lower()
            if k.upper() in _OI_KIND and key == "openinference.span.kind":
                return _OI_KIND[k.upper()], True
            if k in table:
                return table[k], True
    op = a.get("gen_ai.operation.name")
    if op in _OP_KIND:
        return _OP_KIND[op], True
    if "gen_ai.tool.name" in a or "tool.name" in a:
        return "tool", True
    if any(k.startswith("gen_ai.request.model") or k.startswith("llm.model_name") for k in a):
        return "llm", True
    low = name.lower()
    for prefix, kind in (("invoke_agent", "agent"), ("execute_tool", "tool"), ("chat ", "llm"), ("embeddings", "embedding")):
        if low.startswith(prefix):
            return kind, True
    return None, False


def _io(a: Dict[str, Any], events: List[Tuple[str, Dict[str, Any]]]) -> Tuple[Any, Any]:
    inp = out = None
    for k in ("gen_ai.input.messages", "gen_ai.prompt", "input.value", "traceloop.entity.input",
              "langfuse.observation.input", "gen_ai.tool.call.arguments", "tool.parameters", "tdn.input"):
        if a.get(k) is not None:
            inp = maybe_json(a[k])
            break
    if inp is None:
        inp = (_indexed_messages(a, "gen_ai.prompt", "role", "content")
               or _indexed_messages(a, "llm.input_messages", "role", "content"))
    for k in ("gen_ai.output.messages", "gen_ai.completion", "output.value", "traceloop.entity.output",
              "langfuse.observation.output", "gen_ai.tool.call.result", "tdn.output"):
        if a.get(k) is not None:
            out = maybe_json(a[k])
            break
    if out is None:
        out = (_indexed_messages(a, "gen_ai.completion", "role", "content")
               or _indexed_messages(a, "llm.output_messages", "role", "content"))
    # span events (older GenAI conventions put content there)
    msgs, choices = [], []
    for name, ea in events:
        if name == "gen_ai.content.prompt" and inp is None:
            inp = maybe_json(ea.get("gen_ai.prompt"))
        elif name == "gen_ai.content.completion" and out is None:
            out = maybe_json(ea.get("gen_ai.completion"))
        elif name in ("gen_ai.system.message", "gen_ai.user.message", "gen_ai.assistant.message", "gen_ai.tool.message"):
            body = maybe_json(ea.get("body", ea.get("content")))
            role = name.split(".")[1]
            msgs.append({"role": role, "content": body.get("content") if isinstance(body, dict) else body})
        elif name == "gen_ai.choice":
            body = maybe_json(ea.get("body", ea.get("message")))
            choices.append(body.get("message", body) if isinstance(body, dict) else body)
    if inp is None and msgs:
        inp = msgs
    if out is None and choices:
        out = choices
    return inp, out


def _error(s: Dict[str, Any], a: Dict[str, Any], events: List[Tuple[str, Dict[str, Any]]]) -> Optional[str]:
    st = s.get("status") or {}
    code = st.get("code", 0)
    is_err = code in (2, "STATUS_CODE_ERROR", "ERROR")
    if str(a.get("langfuse.observation.level", "")).upper() == "ERROR":
        is_err = True
    exc = next((ea for n, ea in events if n == "exception"), None)
    if not is_err:
        return None
    msg = st.get("message") or a.get("langfuse.observation.status_message") or ""
    if exc:
        detail = ": ".join(x for x in (exc.get("exception.type"), exc.get("exception.message")) if x)
        msg = msg or detail
    return msg or "error"


def spans_from_otlp(bodies: Iterable[Dict[str, Any]]) -> Dict[str, List[Tuple[Dict[str, Any], Dict[str, Any]]]]:
    """Group raw OTLP spans by trace: trace_id -> [(span, resource attrs)]."""
    by_trace: Dict[str, List[Tuple[Dict[str, Any], Dict[str, Any]]]] = defaultdict(list)
    for body in bodies:
        for rs in body.get("resourceSpans", []) or []:
            res = attrs_of((rs.get("resource") or {}).get("attributes"))
            for ss in rs.get("scopeSpans", rs.get("instrumentationLibrarySpans", [])) or []:
                for s in ss.get("spans", []) or []:
                    by_trace[norm_id(s.get("traceId"), 16)].append((s, res))
    return by_trace


_COST_KEYS = ("tdn.llm.cost_usd", "gen_ai.usage.cost", "llm.cost.total", "gen_ai.usage.cost_usd")
_CACHE_READ_KEYS = ("gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.cache_read_input_tokens",
                    "llm.token_count.prompt_details.cache_read")
_CACHE_WRITE_KEYS = ("gen_ai.usage.cache_creation.input_tokens", "gen_ai.usage.cache_creation_input_tokens",
                     "llm.token_count.prompt_details.cache_write")


def _cost_attrs(a: Dict[str, Any]) -> Dict[str, Any]:
    """Reported cost (OTel GenAI, OpenInference, Langfuse OTel) and cache token counts."""
    out: Dict[str, Any] = {}

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    cost = next((num(a[k]) for k in _COST_KEYS if num(a.get(k)) is not None), None)
    if cost is None and a.get("langfuse.observation.cost_details"):
        cd = maybe_json(a["langfuse.observation.cost_details"])
        if isinstance(cd, dict):
            cost = num(cd.get("total")) if num(cd.get("total")) is not None else \
                (sum(v for v in (num(x) for x in cd.values()) if v is not None) or None)
    if cost is not None and cost >= 0:
        out["tdn.llm.cost_usd"] = cost
    for keys, dest in ((_CACHE_READ_KEYS, "gen_ai.usage.cache_read.input_tokens"),
                       (_CACHE_WRITE_KEYS, "gen_ai.usage.cache_creation.input_tokens")):
        v = next((num(a[k]) for k in keys if num(a.get(k)) is not None), None)
        if v is not None:
            out[dest] = int(v)
    return out


def node_from_span(s: Dict[str, Any]) -> Node:
    a = attrs_of(s.get("attributes"))
    events = [(e.get("name", ""), attrs_of(e.get("attributes"))) for e in s.get("events", []) or []]
    name = s.get("name") or "span"
    kind, _ = _kind(name, a)
    inp, out = _io(a, events)
    tool = a.get("gen_ai.tool.name") or a.get("tool.name") or (a.get("traceloop.entity.name") if kind == "tool" else None)
    agent = a.get("gen_ai.agent.name") or a.get("agent.name") or (
        a.get("traceloop.entity.name") if kind == "agent" else None)
    model = a.get("gen_ai.request.model") or a.get("gen_ai.response.model") or a.get("llm.model_name")
    tin = a.get("gen_ai.usage.input_tokens", a.get("gen_ai.usage.prompt_tokens", a.get("llm.token_count.prompt")))
    tout = a.get("gen_ai.usage.output_tokens", a.get("gen_ai.usage.completion_tokens", a.get("llm.token_count.completion")))
    keep = {k: maybe_json(v) for k, v in a.items() if k.startswith(("tdn.", "gen_ai.agent", "gen_ai.system", "gen_ai.provider"))
            and k not in ("tdn.kind", "tdn.step", "tdn.input.digest", "tdn.output.digest")}
    keep.update(_cost_attrs(a))
    return Node(
        id=norm_id(s.get("spanId"), 8), parent=norm_id(s.get("parentSpanId"), 8) or None, name=name, kind=kind,
        start=int(s.get("startTimeUnixNano") or 0), end=int(s.get("endTimeUnixNano") or 0) or None,
        input=inp, output=out, error=_error(s, a, events), attrs=keep, agent=agent, tool=tool, model=model,
        tokens_in=int(tin) if isinstance(tin, (int, float, str)) and str(tin).isdigit() else None,
        tokens_out=int(tout) if isinstance(tout, (int, float, str)) and str(tout).isdigit() else None,
        step=a.get("tdn.step") if isinstance(a.get("tdn.step"), int) else None,
        input_digest=a.get("tdn.input.digest"), output_digest=a.get("tdn.output.digest"))


def import_otlp(bodies: Iterable[Dict[str, Any]], failed: Optional[bool] = None
                ) -> List[Tuple[str, List[Dict[str, Any]], Dict[str, str]]]:
    """OTLP/JSON bodies -> [(trace_id, spans, blobs)], one per trace."""
    runs = []
    for trace_id, items in spans_from_otlp(bodies).items():
        nodes = [node_from_span(s) for s, _ in items]
        res = items[0][1] if items else {}
        name = res.get("service.name")
        top = [n for n in nodes if n.parent is None or n.parent not in {m.id for m in nodes}]
        if len(top) == 1 and top[0].kind is None:
            top[0].kind = "agent"
        spans, blobs = build_run(trace_id, nodes, run_name=name, failed=failed, source="otlp")
        runs.append((trace_id, spans, blobs))
    return runs


def load_otlp_file(path: str) -> List[Dict[str, Any]]:
    """An OTLP/JSON export: one object, a list of objects, or one object per line."""
    text = open(path, encoding="utf-8").read().strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except ValueError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
