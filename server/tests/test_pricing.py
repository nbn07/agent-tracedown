"""Dollar costs: price lookup, overrides, span and run cost, API fields, importers."""
import datetime
import json

import pytest
from fastapi.testclient import TestClient

import agent_tracedown as tdn
from tracedown_server import pricing
from tracedown_server.api import create_app
from tracedown_server.storage import SQLiteStore
from tracedown_server.importers.langfuse import node_from_observation
from tracedown_server.importers.otel import node_from_span

P = pricing.DEFAULT_PRICES


@pytest.fixture()
def env():
    mem = tdn.MemoryExporter()
    tdn.init(project="demo", exporter=mem)
    app = create_app(store=SQLiteStore(":memory:"), api_keys=[], queue=False)
    yield TestClient(app), mem, None, app
    tdn.init(project="default")


@pytest.mark.parametrize("name,expected", [
    ("claude-sonnet-4-5", "claude-sonnet-4-5"),
    ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
    ("anthropic/claude-opus-5-5", "claude-opus-5-5"),
    ("anthropic:claude-haiku-5-5", "claude-haiku-5-5"),
    ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5"),
    ("claude-3-5-haiku-20241022", "claude-haiku-3-5"),
    ("claude-opus-4-0", "claude-opus-4"),
    ("claude-opus-4-20250514", "claude-opus-4"),
    ("Claude-Opus-4.5", "claude-opus-4-5"),
    ("gpt-4o-mini-2024-07-18", "gpt-4o-mini"),
    ("openai/gpt-4o", "gpt-4o"),
    ("models/gemini-2.5-pro", "gemini-2.5-pro"),
    ("gpt-5-mini-2025-08-07", "gpt-5-mini"),
])
def test_lenient_model_names(name, expected):
    assert pricing.price_for(P, name) is P[expected]


@pytest.mark.parametrize("name", ["gpt-5.6-terra", "claude-sonnet-4-9", "my-finetune", "", None, "gpt-4", "o1"])
def test_unknown_models_are_not_guessed(name):
    assert pricing.price_for(P, name) is None


def test_variant_falls_back_to_base_model():
    assert pricing.price_for(P, "gpt-4o-mini-search-preview") is P["gpt-4o-mini"]


def test_every_default_entry_is_valid():
    pricing.validate(P)
    assert all(v["input"] > 0 and v["output"] > 0 for v in P.values())


def test_token_cost_and_reported_cost_wins():
    call = {"model": "gpt-4o-mini", "input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert pricing.call_cost(call, P) == (pytest.approx(0.75), "computed")
    assert pricing.call_cost({**call, "cost_usd": 0.01}, P) == (0.01, "reported")
    assert pricing.call_cost({"model": "nope", "input_tokens": 5}, P) == (None, None)


def test_cache_tokens_anthropic_vs_openai():
    # Anthropic: input_tokens excludes cache tokens; prompt length includes them
    a = {"model": "claude-sonnet-4-5", "input_tokens": 1000, "output_tokens": 0,
         "cache_read_tokens": 10_000, "cache_write_tokens": 2000}
    assert pricing.call_cost(a, P)[0] == pytest.approx((1000 * 3 + 10_000 * 0.30 + 2000 * 3.75) / 1e6)
    # OpenAI: cached tokens are inside input_tokens
    o = {"model": "gpt-4o", "input_tokens": 10_000, "output_tokens": 0, "cache_read_tokens": 4000}
    assert pricing.call_cost(o, P)[0] == pytest.approx((6000 * 2.5 + 4000 * 1.25) / 1e6)


def test_long_prompt_tiers():
    small = {"model": "claude-haiku-5-5", "input_tokens": 100_000, "output_tokens": 1000}
    big = {**small, "input_tokens": 50_000, "cache_read_tokens": 60_000}  # 110k prompt: over 100k
    assert pricing.call_cost(small, P)[0] == pytest.approx((100_000 * 0.10 + 1000 * 0.50) / 1e6)
    assert pricing.call_cost(big, P)[0] == pytest.approx((50_000 * 0.50 + 60_000 * 0.05 + 1000 * 2.50) / 1e6)
    g = {"model": "gemini-2.5-pro", "input_tokens": 250_000, "output_tokens": 0}
    assert pricing.call_cost(g, P)[0] == pytest.approx(250_000 * 2.5 / 1e6)


def test_dated_price_change_uses_call_time():
    call = {"model": "gemini-3.8-flash", "input_tokens": 1_000_000, "output_tokens": 0}
    ns = lambda d: int(datetime.datetime.fromisoformat(d + "T12:00:00+00:00").timestamp()) * 10**9  # noqa: E731
    dec_2026, jan_2027 = ns("2026-12-31"), ns("2027-01-01")
    assert pricing.call_cost(call, P, dec_2026)[0] == pytest.approx(0.75)
    assert pricing.call_cost(call, P, jan_2027)[0] == pytest.approx(1.5)


def test_overrides_env_then_saved(monkeypatch, tmp_path):
    monkeypatch.setenv("TDN_PRICES", json.dumps({"gpt-4o": {"input": 1, "output": 1}, "mine": {"input": 2, "output": 2}}))
    m = pricing.merged({"mine": {"input": 3, "output": 3}})
    assert m["gpt-4o"]["input"] == 1 and m["mine"]["input"] == 3 and "claude-opus-5-5" in m
    f = tmp_path / "prices.json"
    f.write_text(json.dumps({"house-model": {"input": 0.5, "output": 0.5}}))
    monkeypatch.setenv("TDN_PRICES", str(f))
    assert pricing.merged()["house-model"]["output"] == 0.5


@pytest.mark.parametrize("bad", [
    {"m": {"input": 1}}, {"m": {"input": -1, "output": 1}}, {"m": {"input": 1, "output": 1, "cache_read": "x"}},
    {"m": {"input": 1, "output": 1, "tiers": [{"above": 0, "input": 2}]}},
    {"m": {"input": 1, "output": 1, "changes": [{"from": "soon", "input": 2}]}},
])
def test_validation(bad):
    with pytest.raises(ValueError):
        pricing.validate(bad)


def _span(kind, attrs, parent="r", start=0):
    return {"kind": kind, "attributes": attrs, "parent_span_id": parent, "start_time": start}


def test_run_cost_from_spans_partial_and_reported():
    spans = [_span("agent", {}, None),
             _span("llm", {"gen_ai.request.model": "gpt-4o-mini", "gen_ai.usage.input_tokens": 1_000_000,
                           "gen_ai.usage.output_tokens": 0}),
             _span("llm", {"gen_ai.request.model": "who-knows", "gen_ai.usage.input_tokens": 10}),
             _span("tool", {})]
    c = pricing.run_cost_from_spans(spans, P)
    assert c == {"cost_usd": pytest.approx(0.15), "cost_complete": False, "unpriced_calls": 1}
    spans[0]["attributes"]["tdn.run.cost_usd"] = 0.42
    assert pricing.run_cost_from_spans(spans, P) == {"cost_usd": 0.42, "cost_complete": True, "unpriced_calls": 0}
    assert pricing.run_cost_from_spans([_span("agent", {}, None)], P)["cost_usd"] == 0.0


def test_api_runs_list_and_detail_show_cost(env):
    client, mem, _, _ = env
    with tdn.run("Coster", input="hi") as run:
        tdn.llm_call(lambda p, **_: {"text": "a", "usage": {"input_tokens": 2000, "output_tokens": 500}},
                     prompt="hi", model="claude-sonnet-4-5-20250929", provider="anthropic")
        tdn.llm_call(lambda p, **_: {"text": "b"}, prompt="hi", model="m", cost_usd=0.25)
        tdn.tool_call("t", {}, lambda: 1)
        run.output("ok")
    client.post("/v1/ingest/blobs", json={"blobs": mem.blobs})
    client.post("/v1/ingest/spans", json={"spans": mem.spans})
    expected = (2000 * 3 + 500 * 15) / 1e6 + 0.25
    row = next(r for r in client.get("/api/v1/runs").json()["runs"] if r["agent_name"] == "Coster")
    assert row["cost_usd"] == pytest.approx(expected) and row["cost_complete"] is True
    detail = client.get(f"/api/v1/runs/{row['trace_id']}").json()
    assert detail["run"]["cost_usd"] == pytest.approx(expected)
    llm = [s for s in detail["spans"] if s["kind"] == "llm"]
    assert sorted(s["cost_source"] for s in llm) == ["computed", "reported"]
    assert sum(s["cost_usd"] for s in llm) == pytest.approx(expected)
    assert all("cost_usd" not in s for s in detail["spans"] if s["kind"] != "llm")


def test_api_saved_prices_override_and_list(env):
    client, mem, _, _ = env
    with tdn.run("Coster2", input="hi") as run:
        tdn.llm_call(lambda p, **_: {"text": "a", "usage": {"input_tokens": 1000, "output_tokens": 0}},
                     prompt="hi", model="house-llm")
        run.output("ok")
    client.post("/v1/ingest/blobs", json={"blobs": mem.blobs})
    client.post("/v1/ingest/spans", json={"spans": mem.spans})
    row = client.get("/api/v1/runs?agent=Coster2").json()["runs"][0]
    assert row["cost_usd"] is None and row["unpriced_calls"] == 1
    assert client.put("/api/v1/settings/prices", json={"house-llm": {"input": 10, "output": 10}}).status_code == 200
    row = client.get("/api/v1/runs?agent=Coster2").json()["runs"][0]
    assert row["cost_usd"] == pytest.approx(0.01) and row["cost_complete"] is True
    assert client.get("/api/v1/settings/prices/overrides").json() == {"house-llm": {"input": 10, "output": 10}}
    allp = client.get("/api/v1/settings/prices").json()
    assert "house-llm" in allp and "claude-opus-5-5" in allp


def test_importers_map_reported_cost():
    o = {"id": "g1", "type": "GENERATION", "name": "chat", "model": "gpt-4o", "startTime": "2026-10-01T00:00:00Z",
         "calculatedTotalCost": 0.0123, "usageDetails": {"input": 100, "output": 5, "cache_read_input_tokens": 40}}
    n = node_from_observation(o)
    assert n.attrs["tdn.llm.cost_usd"] == 0.0123 and n.attrs["gen_ai.usage.cache_read.input_tokens"] == 40
    o2 = {**o, "calculatedTotalCost": None, "costDetails": {"input": 0.01, "output": 0.002}}
    assert node_from_observation(o2).attrs["tdn.llm.cost_usd"] == pytest.approx(0.012)

    def kv(k, v):
        return {"key": k, "value": {"doubleValue": v} if isinstance(v, float) else {"intValue": str(v)}}
    s = {"traceId": "a" * 32, "spanId": "b" * 16, "name": "chat gpt-4o", "startTimeUnixNano": "1",
         "attributes": [{"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
                        kv("gen_ai.usage.cost", 0.5), kv("llm.token_count.prompt_details.cache_read", 7)]}
    n = node_from_span(s)
    assert n.attrs["tdn.llm.cost_usd"] == 0.5 and n.attrs["gen_ai.usage.cache_read.input_tokens"] == 7
