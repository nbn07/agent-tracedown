"""What each model call cost, in US dollars.

Cost of one LLM step, in order of preference:

1. the cost the caller reported (``tdn.llm.cost_usd``: the SDK's ``cost_usd=``, or a cost an
   importer found in Langfuse / OpenTelemetry data). Exact, so it always wins;
2. tokens × the price of the model, from the table below, overridden by ``TDN_PRICES`` and
   by ``PUT /api/v1/settings/prices``.

A run's cost is ``tdn.run.cost_usd`` on its root span when an adapter reports one, else the
sum of its steps. Steps on a model with no known price are counted, not guessed: the run
then reports the cost of the steps that are priced and how many were not.

Prices are USD per million tokens. An entry is::

    {"input": 2.0, "output": 10.0,          # required
     "cache_read": 0.2,                     # cache hits (optional)
     "cache_write": 2.5,                    # cache writes, 5-minute TTL (optional)
     "cache_write_1h": 4.0,                 # cache writes, 1-hour TTL (optional)
     "tiers": [{"above": 200000, "input": 4.0, "output": 18.0}],   # long-prompt prices
     "changes": [{"from": "2027-01-01", "input": 1.5, "output": 7.5}]}  # dated price changes

``tiers`` apply when the prompt (input plus cache tokens) is above ``above`` tokens.
``changes`` apply to calls made on or after ``from`` (UTC), so old runs keep the price they
were charged.

Token counts. ``gen_ai.usage.input_tokens`` is the uncached input for Anthropic (its API
reports cache tokens separately) and all input including cache hits for OpenAI and Google
(their APIs count cached tokens inside the prompt). Cache tokens are read from
``gen_ai.usage.cache_read.input_tokens`` / ``gen_ai.usage.cache_creation.input_tokens``.

Sources, checked 2026-10-31 (list prices, standard tier; check them for your account and
override anything that differs):

* Anthropic: https://platform.claude.com/docs/en/about-claude/pricing
* OpenAI: https://developers.openai.com/api/docs/pricing and the per-model pages under
  https://developers.openai.com/api/docs/models/ (older models are priced only there)
* Google: https://ai.google.dev/gemini-api/docs/pricing (paid tier, standard)

Models whose price could not be confirmed on those pages are left out on purpose.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

PRICES_CHECKED = "2026-10-31"


def _a(inp, out, w5, w1h, read, **extra):
    """Anthropic entry: input, output, 5-minute cache write, 1-hour cache write, cache read."""
    return {"input": inp, "output": out, "cache_write": w5, "cache_write_1h": w1h, "cache_read": read, **extra}


def _o(inp, cached, out, **extra):
    """OpenAI / Google entry: input, cached input, output."""
    return {"input": inp, "cache_read": cached, "output": out, **extra}


DEFAULT_PRICES: Dict[str, Dict[str, Any]] = {
    # ---------------------------------------------------------------- Anthropic
    "claude-fable-5-1": _a(10, 50, 12.5, 20, 0.25),
    "claude-mythos-5-1": _a(10, 50, 12.5, 20, 0.25),
    "claude-opus-5-5": _a(4, 20, 5, 8, 0.20),
    "claude-sonnet-5-5": _a(2, 10, 2.5, 4, 0.10),
    "claude-haiku-5-5": _a(0.10, 0.50, 0.125, 0.20, 0.01,
                           tiers=[{"above": 100_000, "input": 0.50, "output": 2.50, "cache_write": 0.625,
                                   "cache_write_1h": 1.0, "cache_read": 0.05}]),
    "claude-fable-5": _a(10, 50, 12.5, 20, 1),
    "claude-mythos-5": _a(10, 50, 12.5, 20, 1),
    "claude-opus-5": _a(5, 25, 6.25, 10, 0.50),
    "claude-opus-4-8": _a(5, 25, 6.25, 10, 0.50),
    "claude-opus-4-7": _a(5, 25, 6.25, 10, 0.50),
    "claude-opus-4-6": _a(5, 25, 6.25, 10, 0.50),
    "claude-opus-4-5": _a(5, 25, 6.25, 10, 0.50),
    "claude-opus-4-1": _a(15, 75, 18.75, 30, 1.50),
    "claude-opus-4": _a(15, 75, 18.75, 30, 1.50),
    "claude-sonnet-5": _a(2, 10, 2.5, 4, 0.20),
    "claude-sonnet-4-6": _a(3, 15, 3.75, 6, 0.30),
    "claude-sonnet-4-5": _a(3, 15, 3.75, 6, 0.30),
    "claude-sonnet-4": _a(3, 15, 3.75, 6, 0.30),
    "claude-haiku-4-5": _a(1, 5, 1.25, 2, 0.10),
    "claude-haiku-3-5": _a(0.80, 4, 1, 1.6, 0.08),
    # ---------------------------------------------------------------- OpenAI
    "gpt-6-astra": _o(10, 1, 50),
    "gpt-6.1-sol": _o(2, 0.10, 10),
    "gpt-6.1-terra": _o(1, 0.05, 5),
    "gpt-6-luna": _o(0.10, 0.01, 0.50),
    "gpt-5.6-sol": _o(4, 0.40, 20),
    "chat-latest": _o(5, 0.50, 30),
    "gpt-5.4": _o(2.50, 0.25, 15, tiers=[{"above": 272_000, "input": 5.0, "cache_read": 0.50, "output": 22.5}]),
    "gpt-5.4-mini": _o(0.75, 0.075, 4.50),
    "gpt-5.3-codex": _o(1.75, 0.175, 14),
    "gpt-5.2": _o(1.75, 0.175, 14),
    "gpt-5.1": _o(1.25, 0.125, 10),
    "gpt-5": _o(1.25, 0.125, 10),
    "gpt-5-mini": _o(0.25, 0.025, 2),
    "gpt-5-nano": _o(0.05, 0.005, 0.40),
    "gpt-4.1": _o(2, 0.50, 8),
    "gpt-4.1-mini": _o(0.40, 0.10, 1.60),
    "gpt-4.1-nano": _o(0.10, 0.025, 0.40),
    "gpt-4o": _o(2.50, 1.25, 10),
    "gpt-4o-mini": _o(0.15, 0.075, 0.60),
    "o3": _o(2, 0.50, 8),
    "o3-mini": _o(1.10, 0.55, 4.40),
    "o4-mini": _o(1.10, 0.275, 4.40),
    # ---------------------------------------------------------------- Google
    "gemini-4-argon": _o(2, 0.10, 10, changes=[{"from": "2026-12-01", "input": 4, "cache_read": 0.20, "output": 20}]),
    "gemini-3.8-flash": _o(0.75, 0.075, 3.75, changes=[{"from": "2027-01-01", "input": 1.5, "cache_read": 0.15,
                                                        "output": 7.5}]),
    "gemini-3.6-flash": _o(0.75, 0.075, 3.75, changes=[{"from": "2027-01-01", "input": 1.5, "cache_read": 0.15,
                                                        "output": 7.5}]),
    "gemini-3.5-flash-lite": _o(0.30, 0.03, 2.50),
    "gemini-3.1-flash-lite": _o(0.25, 0.025, 1.50),
    "gemini-3.1-pro-preview": _o(2, 0.20, 12, tiers=[{"above": 200_000, "input": 4, "cache_read": 0.40, "output": 18}]),
    "gemini-3-flash-preview": _o(0.50, 0.05, 3),
    "gemini-2.5-pro": _o(1.25, 0.125, 10, tiers=[{"above": 200_000, "input": 2.5, "cache_read": 0.25, "output": 15}]),
    "gemini-2.5-flash": _o(0.30, 0.03, 2.50),
}

PRICE_FIELDS = ("input", "output", "cache_read", "cache_write", "cache_write_1h")


# ----------------------------------------------------------------------------- the price table

def validate(prices: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Check a user price map; raise ValueError with a readable message."""
    if not isinstance(prices, dict):
        raise ValueError("prices must be a JSON object of model -> prices")

    def check(model: str, p: Dict[str, Any], need_io: bool) -> None:
        if not isinstance(p, dict):
            raise ValueError(f"{model}: price entry must be an object")
        if need_io and not {"input", "output"} <= set(p):
            raise ValueError(f"{model}: need non-negative 'input' and 'output' USD per million tokens")
        for k in PRICE_FIELDS:
            if k in p and (isinstance(p[k], bool) or not isinstance(p[k], (int, float)) or p[k] < 0):
                raise ValueError(f"{model}: '{k}' must be a non-negative number (USD per million tokens)")

    for model, p in prices.items():
        check(model, p, True)
        for t in p.get("tiers") or []:
            check(model, t, False)
            if not isinstance(t.get("above"), int) or t["above"] <= 0:
                raise ValueError(f"{model}: each tier needs a positive integer 'above' (prompt tokens)")
        for c in p.get("changes") or []:
            check(model, c, False)
            try:
                _dt.date.fromisoformat(str(c.get("from")))
            except ValueError as exc:
                raise ValueError(f"{model}: each change needs 'from' as YYYY-MM-DD") from exc
    return prices


def env_prices() -> Dict[str, Dict[str, Any]]:
    """``TDN_PRICES``: inline JSON, or a path to a JSON file."""
    raw = os.environ.get("TDN_PRICES", "").strip()
    if not raw:
        return {}
    if not raw.startswith("{"):
        with open(raw, encoding="utf-8") as fh:
            raw = fh.read()
    return validate(json.loads(raw))


def merged(user: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Dict[str, Any]]:
    """Built-in table, then ``TDN_PRICES``, then prices saved through the API (later wins)."""
    out = {k: dict(v) for k, v in DEFAULT_PRICES.items()}
    for layer in (env_prices(), user or {}):
        for k, v in layer.items():
            out[k] = dict(v)
    return out


# ----------------------------------------------------------------------------- model names

_DATE = re.compile(r"(?:[-@_]\d{8}|[-@_]\d{4}-\d{2}-\d{2})(?=$|[-:@])")
_PREFIX = re.compile(r"^(?:[a-z0-9_-]+/)+|^(?:anthropic|openai|google|gemini|vertex_ai|azure|bedrock):")
_BEDROCK = re.compile(r"^(?:[a-z]{2,4}\.)?anthropic\.")


def normalize(model: str) -> str:
    """'anthropic/claude-3-5-haiku-20241022' -> 'claude-haiku-3-5'; 'models/gemini-2.5-pro' -> 'gemini-2.5-pro';
    'us.anthropic.claude-sonnet-4-5-20250929-v1:0' -> 'claude-sonnet-4-5'; 'gpt-4o-mini-2024-07-18' -> 'gpt-4o-mini'."""
    m = model.strip().lower()
    m = _PREFIX.sub("", m)
    m = _BEDROCK.sub("", m)
    m = re.sub(r"-v\d+(?::\d+)?$", "", m)  # Bedrock version suffix
    m = _DATE.sub("", m)
    m = re.sub(r"-latest$", "", m)
    if m.startswith("claude"):
        m = m.replace(".", "-")
        old = re.match(r"^claude-(\d)-(\d)-(opus|sonnet|haiku)(.*)$", m)  # claude-3-5-haiku -> claude-haiku-3-5
        if old:
            m = f"claude-{old.group(3)}-{old.group(1)}-{old.group(2)}{old.group(4)}"
        m = re.sub(r"-0$", "", m)  # claude-opus-4-0 -> claude-opus-4
    return m


def price_for(prices: Dict[str, Dict[str, Any]], model: Optional[str]) -> Optional[Dict[str, Any]]:
    """Find the price entry for a model name as an SDK or framework reported it."""
    if not model:
        return None
    if model in prices:
        return prices[model]
    norm = normalize(model)
    by_norm = {normalize(k): v for k, v in prices.items()}
    if norm in by_norm:
        return by_norm[norm]
    # A variant of a known model ('gpt-4o-mini-search-preview'): the longest known name it
    # extends, but never a different version ('gpt-5' must not price 'gpt-5.6-terra' or 'claude-sonnet-4'
    # 'claude-sonnet-4-9'), so the next character has to start a word, not a number.
    best = max((k for k in by_norm if norm.startswith(k + "-") and not norm[len(k) + 1:len(k) + 2].isdigit()),
               key=len, default=None)
    return by_norm[best] if best else None


# ----------------------------------------------------------------------------- costs

def _num(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _anthropic(model: Optional[str], provider: Optional[str]) -> bool:
    return (provider or "").lower() in ("anthropic", "aws.bedrock", "claude") or normalize(model or "").startswith("claude")


def call_usage(attrs: Dict[str, Any]) -> Dict[str, Any]:
    """Model, provider and token counts of one LLM step, from its span attributes."""
    return {
        "model": attrs.get("gen_ai.response.model") or attrs.get("gen_ai.request.model"),
        "provider": attrs.get("gen_ai.provider.name") or attrs.get("gen_ai.system"),
        "input_tokens": _num(attrs.get("gen_ai.usage.input_tokens")),
        "output_tokens": _num(attrs.get("gen_ai.usage.output_tokens")),
        "cache_read_tokens": _num(attrs.get("gen_ai.usage.cache_read.input_tokens")
                                  or attrs.get("gen_ai.usage.cache_read_input_tokens")),
        "cache_write_tokens": _num(attrs.get("gen_ai.usage.cache_creation.input_tokens")
                                   or attrs.get("gen_ai.usage.cache_creation_input_tokens")),
        "cost_usd": attrs.get("tdn.llm.cost_usd"),
    }


def _effective(entry: Dict[str, Any], prompt_tokens: int, at_ns: Optional[int]) -> Dict[str, Any]:
    p = dict(entry)
    if at_ns and p.get("changes"):
        day = _dt.datetime.fromtimestamp(at_ns / 1e9, _dt.timezone.utc).date().isoformat()
        for ch in sorted(p["changes"], key=lambda c: str(c["from"])):
            if day >= str(ch["from"]):
                p.update({k: v for k, v in ch.items() if k in PRICE_FIELDS})
    for tier in sorted(p.get("tiers") or [], key=lambda t: t["above"]):
        if prompt_tokens > tier["above"]:
            p.update({k: v for k, v in tier.items() if k in PRICE_FIELDS})
    return p


def call_cost(call: Dict[str, Any], prices: Dict[str, Dict[str, Any]],
              at_ns: Optional[int] = None) -> Tuple[Optional[float], Optional[str]]:
    """``(usd, source)`` for one call (``call_usage`` shape). source: 'reported', 'computed' or None."""
    if call.get("cost_usd") is not None:
        try:
            return float(call["cost_usd"]), "reported"
        except (TypeError, ValueError):
            pass
    entry = price_for(prices, call.get("model"))
    if entry is None:
        return None, None
    inp, out = _num(call.get("input_tokens")), _num(call.get("output_tokens"))
    cr, cw = _num(call.get("cache_read_tokens")), _num(call.get("cache_write_tokens"))
    if _anthropic(call.get("model"), call.get("provider")):
        uncached, prompt = inp, inp + cr + cw
    else:  # OpenAI / Google count cached tokens inside the prompt
        uncached, prompt = max(inp - cr - cw, 0), inp
    p = _effective(entry, prompt, at_ns or call.get("at_ns"))
    usd = (uncached * p["input"] + out * p["output"]
           + cr * p.get("cache_read", p["input"]) + cw * p.get("cache_write", p["input"])) / 1e6
    return round(usd, 8), "computed"


def span_cost(span: Dict[str, Any], prices: Dict[str, Dict[str, Any]]) -> Tuple[Optional[float], Optional[str]]:
    if span.get("kind") != "llm":
        return None, None
    return call_cost(call_usage(span.get("attributes") or {}), prices, span.get("start_time"))


def summarize(calls: Iterable[Tuple[Optional[float], Optional[str]]],
              reported_total: Any = None) -> Dict[str, Any]:
    """Run cost from its calls' ``(usd, source)``; ``reported_total`` is ``tdn.run.cost_usd``."""
    calls = list(calls)
    priced = [c for c, _ in calls if c is not None]
    unpriced = len(calls) - len(priced)
    if reported_total is not None:
        try:
            return {"cost_usd": round(float(reported_total), 8), "cost_complete": True, "unpriced_calls": 0}
        except (TypeError, ValueError):
            pass
    return {"cost_usd": round(sum(priced), 8) if priced else (0.0 if not calls else None),
            "cost_complete": unpriced == 0, "unpriced_calls": unpriced}


def run_cost_from_spans(spans: List[Dict[str, Any]], prices: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    root = next((s for s in spans if not s.get("parent_span_id")), None)
    reported = (root or {}).get("attributes", {}).get("tdn.run.cost_usd")
    return summarize((span_cost(s, prices) for s in spans if s.get("kind") == "llm"), reported)


def run_cost_from_profile(profile: Optional[Dict[str, Any]], prices: Dict[str, Dict[str, Any]],
                          at_ns: Optional[int] = None) -> Dict[str, Any]:
    """Same as ``run_cost_from_spans`` for a stored run profile (``drift.build_profile``)."""
    if not profile:
        return {"cost_usd": None, "cost_complete": False, "unpriced_calls": 0}
    f = profile.get("features") or {}
    return summarize((call_cost(c, prices, c.get("at_ns") or at_ns) for c in f.get("llm", [])), f.get("run_cost_usd"))


def by_model(profiles: Iterable[Dict[str, Any]], prices: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Calls, tokens and cost per model across run profiles (rows from ``store.run_profiles``)."""
    acc: Dict[str, Dict[str, Any]] = {}
    for r in profiles:
        for c in (r["profile"].get("features") or {}).get("llm", []):
            m = c.get("model") or "unknown"
            e = acc.setdefault(m, {"model": m, "calls": 0, "input_tokens": 0, "output_tokens": 0,
                                   "cost_usd": 0.0, "unpriced_calls": 0})
            e["calls"] += 1
            e["input_tokens"] += _num(c.get("input_tokens"))
            e["output_tokens"] += _num(c.get("output_tokens"))
            usd, _ = call_cost(c, prices, c.get("at_ns") or r.get("started_at"))
            if usd is None:
                e["unpriced_calls"] += 1
            else:
                e["cost_usd"] += usd
    rows = sorted(acc.values(), key=lambda e: -e["cost_usd"])
    for e in rows:
        e["cost_usd"] = round(e["cost_usd"], 6) if e["calls"] > e["unpriced_calls"] else None
    return rows
