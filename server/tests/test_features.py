"""v1.1 hallucination investigator, v1.2 drift, v1.3 incidents + hooks, analytics,
Prometheus, wrong-tool detection, live events and OTLP."""
import json

import pytest
from fastapi.testclient import TestClient

import agent_tracedown as tdn
import demo_agents as demo
from tracedown_server import analytics, drift
from tracedown_server.api import create_app
from tracedown_server.claims import investigate, split_claims
from tracedown_server.incidents import hooks
from tracedown_server.rca import RunView, analyze
from tracedown_server.storage import SQLiteStore


@pytest.fixture()
def mem():
    m = tdn.MemoryExporter()
    tdn.init(project="demo", exporter=m)
    return m


def run_agent(mem, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except tdn.ToolError:
        pass
    tid = mem.spans[-1]["trace_id"]
    return tid, mem.trace(tid)


class Sink:
    """Captures hook deliveries instead of calling Slack/PagerDuty/GitHub."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, headers, body):
        self.calls.append((url, headers, body))
        return 200, "ok"


@pytest.fixture()
def env(mem):
    sink = Sink()
    app = create_app(store=SQLiteStore(":memory:"), api_keys=[], notifier_transport=sink, queue=False)
    return TestClient(app), mem, sink, app


def push(client, mem, project="demo"):
    client.post("/v1/ingest/blobs", json={"project": project, "blobs": mem.blobs})
    r = client.post("/v1/ingest/spans", json={"project": project, "spans": mem.spans})
    mem.clear()
    return r.json()


# ----------------------------------------------------------------------------- v1.1 hallucination investigator

def test_claim_splitting_keeps_offsets():
    text = "The Pro plan costs $49 per month and includes priority support. Let me know if you need anything."
    claims = split_claims(text)
    assert [c.text for c in claims] == ["The Pro plan costs $49 per month", "includes priority support.",
                                        "Let me know if you need anything."]
    assert all(text[c.start:c.end] == c.text for c in claims)


def test_claims_are_labelled_and_attributed(mem):
    _, spans = run_agent(mem, demo.pricing_agent)
    r = investigate(RunView(spans, mem.blobs))
    labels = {c["text"]: c["label"] for c in r["claims"]}
    assert labels["The Pro plan costs $49 per month"] == "contradicted"
    assert labels["includes priority support."] == "unsupported"
    contradicted = r["claims"][0]
    assert contradicted["evidence"][0]["unit"].startswith("tool:get_price") and "39" in contradicted["rationale"]
    assert contradicted["origin"]["step"] == 3  # the LLM step that wrote the answer
    assert r["summary"]["hallucination_score"] == 1.0
    # heatmap: "49" is unsupported, "pro" and "plan" are supported
    seg = {r["answer"][s["start"]:s["end"]].lower(): s["support"] for s in r["segments"] if s["support"] is not None}
    assert seg["$49"] == 0.0 and seg["pro"] == 1.0 and seg["plan"] == 1.0


def test_stale_and_supported_claims(mem):
    _, spans = run_agent(mem, demo.refund_agent)
    r = investigate(RunView(spans, mem.blobs))
    assert [c["label"] for c in r["claims"]] == ["stale", "supported"]
    assert r["claims"][1]["evidence"][0]["source"] == "billing-faq.md"


def test_judge_refines_partial_claims(mem):
    with tdn.run("Bot", input="refund?") as run:
        tdn.retrieval("refund", lambda q: [{"id": "c1", "text": "Refunds are processed by the billing team on request.",
                                            "score": 0.9}])
        run.output("The billing team refunds customers automatically every Friday overnight.")
    seen = []

    def judge(claim, evidence):
        seen.append(claim)
        return {"label": "unsupported", "rationale": "source says on request, not automatically"}

    r = investigate(RunView(mem.trace(run.trace_id), mem.blobs), judge=judge)
    assert seen and r["claims"][0]["label"] == "unsupported" and r["claims"][0]["rationale"].startswith("judge:")


def test_claims_endpoint(env):
    client, mem, _, _ = env
    tid, _ = run_agent(mem, demo.pricing_agent)
    push(client, mem)
    body = client.get(f"/api/v1/runs/{tid}/claims").json()
    assert body["summary"]["contradicted"] == 1 and body["segments"]


# ----------------------------------------------------------------------------- wrong tool

def test_wrong_tool_detected_on_the_failing_step(mem):
    _, spans = run_agent(mem, demo.account_agent)
    r = analyze(spans, mem.blobs)
    assert r["root_cause"]["category"] == "wrong_tool"
    assert "cancel_subscription" in r["root_cause"]["title"]
    assert "root cause: Wrong tool selected" in r["stack_trace"]


# ----------------------------------------------------------------------------- v1.2 drift

def test_drift_report_written_automatically_and_notified(env):
    client, mem, sink, app = env
    hooks.create(app.state.store, "slack", {"url": "https://hooks.slack.test/x"}, ["drift.detected"], "low")
    for i in range(30):
        demo.support_cohort("billing@v3", i)
    push(client, mem)
    assert drift.list_reports(app.state.store) == []  # one value only: nothing to compare
    for i in range(30):
        demo.support_cohort("billing@v4", i)
    push(client, mem)
    reports = client.get("/api/v1/drift/reports?agent=SupportBot").json()["reports"]
    by_dim = {r["dimension"]: r["report"] for r in reports}
    rep = by_dim["prompt_version"]
    assert rep["verdict"] == "drift" and rep["baseline"] == "billing@v3" and rep["candidate"] == "billing@v4"
    assert rep["success_rate"]["b"] < rep["success_rate"]["a"]
    escalate = next(t for t in rep["tool_mix"]["tools"] if t["tool"] == "escalate")
    assert escalate["status"] == "new"
    assert any("tool-call mix shifted" in s for s in rep["signals"])
    assert "release" in by_dim  # the deploy dimension drifted the same way
    slack = [c for c in sink.calls if "slack" in c[0]]
    assert slack and "drift" in slack[0][2]["text"]


def test_drift_compare_endpoint_and_values(env):
    client, mem, _, _ = env
    for i in range(25):
        demo.support_cohort("billing@v3", i)
    for i in range(25):
        demo.support_cohort("billing@v4", i)
    push(client, mem)
    vals = client.get("/api/v1/drift/values?agent=SupportBot&dimension=prompt_version").json()["values"]
    assert [v["value"] for v in vals] == ["billing@v3", "billing@v4"]
    rep = client.get("/api/v1/drift/compare", params={"agent": "SupportBot", "dimension": "prompt_version",
                                                      "a": "billing@v3", "b": "billing@v4"}).json()
    assert rep["runs"] == {"a": 25, "b": 25} and rep["tokens"]["change"] < 0
    same = client.get("/api/v1/drift/compare", params={"agent": "SupportBot", "dimension": "prompt_version",
                                                       "a": "billing@v3", "b": "billing@v3"}).json()
    assert same["verdict"] == "no_drift"
    assert client.get("/api/v1/drift/compare", params={"agent": "SupportBot", "dimension": "bogus",
                                                       "a": "x", "b": "y"}).status_code == 422


def test_z_test_and_jsd():
    assert drift._z_test(90, 100, 60, 100) < 0.001
    assert drift._z_test(50, 100, 50, 100) == 1.0
    assert drift._jsd({"a": 1}, {"a": 1}) == 0 and drift._jsd({"a": 1}, {"b": 1}) == 1.0


# ----------------------------------------------------------------------------- v1.3 incidents

def test_incident_clusters_runs_and_notifies_all_three_services(env):
    client, mem, sink, app = env
    store = app.state.store
    hooks.create(store, "slack", {"url": "https://hooks.slack.test/T/B/x"}, min_severity="low")
    hooks.create(store, "pagerduty", {"routing_key": "R0UTINGKEY123"}, min_severity="medium")
    hooks.create(store, "github", {"repo": "acme/agents", "token": "ghp_test"}, min_severity="low")
    for _ in range(6):
        run_agent(mem, demo.calendar_agent)
        push(client, mem)
    incs = client.get("/api/v1/incidents").json()["incidents"]
    assert len(incs) == 1
    inc = incs[0]
    assert inc["run_count"] == 6 and inc["category"] == "bad_llm_output" and inc["target"] == "gpt-4o-mini"
    assert inc["severity"] == "medium" and "structured output" in inc["suggested_fix"]
    detail = client.get(f"/api/v1/incidents/{inc['id']}").json()
    kinds = [e["kind"] for e in detail["timeline"]["events"]]
    assert kinds[0] == "opened" and "escalated" in kinds and "notified" in kinds
    assert sum(b["runs"] for b in detail["timeline"]["runs_per_hour"]) == 6
    urls = [c[0] for c in sink.calls]
    assert any("hooks.slack.test" in u for u in urls)
    assert any(u == "https://api.github.com/repos/acme/agents/issues" for u in urls)
    pd = [c for c in sink.calls if c[0] == "https://events.pagerduty.com/v2/enqueue"]
    assert pd and pd[0][2]["event_action"] == "trigger" and pd[0][2]["payload"]["severity"] == "warning"
    assert pd[0][2]["dedup_key"] == f"tdn-incident-{inc['id']}"
    gh = next(c for c in sink.calls if "api.github.com" in c[0])
    assert gh[1]["Authorization"] == "Bearer ghp_test" and "Suggested fix" in gh[2]["body"]
    # secrets never come back from the API
    listed = client.get("/api/v1/hooks").json()["hooks"]
    assert all("ghp_test" not in json.dumps(h) and "R0UTINGKEY123" not in json.dumps(h) for h in listed)


def test_resolved_incident_regresses(env):
    client, mem, sink, _ = env
    run_agent(mem, demo.calendar_agent)
    push(client, mem)
    iid = client.get("/api/v1/incidents").json()["incidents"][0]["id"]
    assert client.post(f"/api/v1/incidents/{iid}/status", json={"status": "resolved"}).json()["status"] == "resolved"
    run_agent(mem, demo.calendar_agent)
    push(client, mem)
    inc = client.get(f"/api/v1/incidents/{iid}").json()
    assert inc["status"] == "open" and "regressed" in [e["kind"] for e in inc["timeline"]["events"]]


def test_different_causes_make_different_incidents_and_successes_make_none(env):
    client, mem, _, _ = env
    for fn in (demo.calendar_agent, demo.refund_agent, demo.research_loop_agent, demo.pricing_agent):
        run_agent(mem, fn)
        push(client, mem)
    with tdn.run("Bot", input="hi") as run:
        run.output("hello")
    push(client, mem)
    cats = sorted(i["category"] for i in client.get("/api/v1/incidents").json()["incidents"])
    # the pricing run "succeeded" but contradicted a tool result: a silent failure still opens an incident
    assert cats == ["bad_llm_output", "hallucination", "loop", "stale_source"]


def test_hook_test_endpoint_and_failed_delivery_is_logged(env):
    client, mem, sink, app = env
    h = client.post("/api/v1/hooks", json={"kind": "webhook", "config": {"url": "https://example.test/hook"}}).json()
    assert client.post(f"/api/v1/hooks/{h['id']}/test").json()["results"][0]["ok"]
    assert client.post("/api/v1/hooks", json={"kind": "pagerduty", "config": {}}).status_code == 422

    def broken(url, headers, body):
        raise OSError("connection refused")
    app.state.pipeline.notifier.transport = broken
    res = app.state.pipeline.notifier.notify("incident.opened", {"id": 0, "title": "t", "severity": "high",
                                                                 "run_count": 1, "root_cause": "r"})
    assert res[0]["ok"] is False and "connection refused" in res[0]["error"]


# ----------------------------------------------------------------------------- analytics and Prometheus

def test_analytics_overview_cost_tools_retrieval(env):
    client, mem, _, app = env
    client.put("/api/v1/settings/prices", json={"gpt-4o-mini": {"input": 1.0, "output": 2.0}})
    for fn in (demo.calendar_agent, demo.refund_agent, demo.pricing_agent):
        run_agent(mem, fn)
    with tdn.run("Bot", input="x") as run:
        tdn.llm_call(lambda p, **_: {"text": "y"}, prompt="x", model="m", cost_usd=0.5)
        run.output("y")
    push(client, mem)
    a = client.get("/api/v1/analytics?days=1").json()
    cal = a["agents"]["CalendarAssistant"]
    assert cal["runs"] == 1 and cal["failure_rate"] == 1.0
    assert cal["cost_usd"]["total"] == pytest.approx((182 * 1 + 19 * 2) / 1e6)
    assert a["agents"]["Bot"]["cost_usd"]["total"] == 0.5
    assert a["agents"]["SupportBot"]["cost_usd"]["total"] is None  # claude model has no price configured
    assert a["agents"]["SalesAssistant"]["hallucination_rate"] == 1.0
    tool = next(t for t in a["tools"] if t["tool"] == "calendar_create_event")
    assert tool["calls"] == 1 and tool["error_rate"] == 1.0 and tool["latency_ms"]["p50"] is not None
    retr = a["retrieval"][0]
    assert retr["source"] == "help-center" and retr["stale_rate"] == 1.0 and retr["reached_answer_rate"] == 1.0
    assert client.put("/api/v1/settings/prices", json={"x": {"input": -1, "output": 1}}).status_code == 422


def test_prometheus_exposition(env):
    client, mem, _, _ = env
    run_agent(mem, demo.calendar_agent)
    run_agent(mem, demo.pricing_agent)
    push(client, mem)
    text = client.get("/metrics").text
    assert '# TYPE tdn_runs_total counter' in text
    assert 'tdn_runs_total{project="demo",agent="CalendarAssistant",outcome="failure"} 1' in text
    assert 'tdn_tool_errors_total{tool="calendar_create_event"} 1' in text
    assert 'tdn_hallucination_runs_total{project="demo",agent="SalesAssistant"} 1' in text
    assert 'tdn_incidents_open{severity=' in text


def test_feedback_updates_profile(env):
    client, mem, _, app = env
    tid, _ = run_agent(mem, demo.pricing_agent)
    push(client, mem)
    assert client.post(f"/api/v1/runs/{tid}/feedback", json={"score": 0}).status_code == 200
    a = client.get("/api/v1/analytics?days=1").json()
    assert a["agents"]["SalesAssistant"]["feedback_score"] == 0


# ----------------------------------------------------------------------------- visualization and live

def test_agent_network_across_runs(env):
    client, mem, _, _ = env
    for _ in range(3):
        run_agent(mem, demo.calendar_agent)
    run_agent(mem, demo.calendar_agent, extractor=demo.extract_event_fixed)
    push(client, mem)
    g = client.get("/api/v1/agents/CalendarAssistant/graph").json()
    assert g["runs"] == 4
    edge = next(e for e in g["edges"] if e["target"] == "tool:calendar_create_event")
    assert edge["calls"] == 4 and edge["errors"] == 3 and edge["error_rate"] == 0.75
    assert any(n["id"] == "agent:WriterAgent" for n in g["nodes"])


def test_live_event_bus_receives_streamed_spans(env):
    client, mem, _, app = env
    bus = app.state.pipeline.bus
    sid = bus.subscribe()
    tdn.init(project="demo", exporter=mem, stream=True)
    with tdn.run("Bot", input="x") as run:
        tdn.tool_call("t", {"a": 1}, lambda a: a)
        # mid-run: the tool span was exported already
        assert any(s["kind"] == "tool" for s in mem.spans) and not any(s["parent_span_id"] is None for s in mem.spans)
        run.output("done")
    push(client, mem)
    kinds = [e["type"] for e in bus.drain(sid, timeout=0.1)]
    assert "spans" in kinds and "run" in kinds


def test_otlp_roundtrip_and_forwarding(monkeypatch, env):
    client, mem, _, app = env
    with tdn.run("OtelBot") as run:
        tdn.tool_call("t", {"a": 1}, lambda a: a)
    body = tdn.to_otlp_json(mem.trace(run.trace_id))
    assert client.post("/v1/traces", json=body).json()["accepted"] == 2
    sent = []
    app.state.pipeline.forward = lambda spans: sent.append(spans)
    mem.clear()
    with tdn.run("FwdBot"):
        pass
    push(client, mem)
    assert sent and sent[0][0]["name"] == "invoke_agent FwdBot"


# ----------------------------------------------------------------------------- LLM judge

def _failed_refund_run(mem):
    """A run that fails with no direct evidence: the agent refunds the wrong order."""
    with tdn.run("SupportBot", input="Refund my order #2 please") as run:
        abx_orders = tdn.tool_call("get_orders", {"user": "u1"}, lambda user: {"orders": ["#1", "#2"]})
        tdn.llm_call(lambda p, **_: {"text": "refunding #1"}, prompt=abx_orders, model="m")
        tdn.tool_call("refund_order", {"order_id": "#1"}, lambda order_id: {"ok": True})
        run.output("Your refund is on its way.")
        run.fail("customer: wrong order refunded")
    return run.trace_id


def test_judge_adds_semantic_finding_when_rules_are_weak(mem):
    from tracedown_server.rca.judge import transcript
    tid = _failed_refund_run(mem)
    prompts = []

    def judge(prompt):
        prompts.append(prompt)
        return '{"step": 3, "category": "bad_llm_output", "confidence": 0.9, "explanation": "refunded #1 but the user asked for #2"}'

    rep = analyze(mem.trace(tid), mem.blobs, judge=judge)
    top = rep["root_cause"]
    assert top["detector"] == "llm_judge" and top["category"] == "bad_llm_output" and top["step"] == 3
    assert "TOOL refund_order" in prompts[0] and "Refund my order #2" in prompts[0]
    assert "refunded #1" in " ".join(top["evidence"])


def test_judge_verdict_on_a_plan_is_anchored_on_the_action_that_carried_it_out(mem):
    tid = _failed_refund_run(mem)
    plan = '{"step": 2, "category": "%s", "confidence": 0.8, "explanation": "chose order #1"}'
    top = analyze(mem.trace(tid), mem.blobs, judge=lambda p: plan % "bad_llm_output")["root_cause"]
    assert top["detector"] == "llm_judge" and top["step"] == 3
    assert top["evidence"][0] == "plan first proposed at step 2; carried out at step 3"
    # a verdict about what the agent *said* stays on the reply
    top = analyze(mem.trace(tid), mem.blobs, judge=lambda p: plan % "hallucination")["root_cause"]
    assert top["step"] == 2


def test_judge_not_called_when_rules_have_direct_evidence(mem):
    try:
        demo.calendar_agent()
    except tdn.ToolError:
        pass
    tid = mem.spans[-1]["trace_id"]
    called = []
    rep = analyze(mem.trace(tid), mem.blobs, judge=lambda p: called.append(p) or "{}")
    assert called == [] and rep["root_cause"]["category"] == "bad_llm_output"


def test_broken_or_nonsense_judge_never_breaks_analysis(mem):
    tid = _failed_refund_run(mem)

    def down(prompt):
        raise OSError("connection refused")

    rep = analyze(mem.trace(tid), mem.blobs, judge=down)
    assert rep["root_cause"]["detector"] != "llm_judge"
    rep2 = analyze(mem.trace(tid), mem.blobs, judge=lambda p: "I think step nine maybe")
    assert all(f["detector"] != "llm_judge" for f in rep2["findings"])
    rep3 = analyze(mem.trace(tid), mem.blobs, judge=lambda p: '{"step": 999, "category": "wrong_tool"}')
    assert all(f["detector"] != "llm_judge" for f in rep3["findings"])


def test_point_of_no_return_on_outcome_failure(mem):
    tid = _failed_refund_run(mem)
    rep = analyze(mem.trace(tid), mem.blobs, judge=None)
    assert rep["root_cause"]["category"] == "unverified_action" and rep["root_cause"]["step"] == 3


# ----------------------------------------------------------------------------- who is at fault

def _support_run(mem, turns, fail="customer goal not met"):
    """turns: list of ("user", text) | ("agent", text) | ("tool", name, args, result_or_Exception)."""
    t = tdn.ManualTrace("SupportBot", input=turns[0][1])
    t.declare_tools([{"name": "find_user_id_by_email", "side_effects": False},
                     {"name": "cancel_order", "side_effects": True}])
    pending, k = [], 0
    for turn in turns:
        k += 1
        if turn[0] == "user":
            pending.append({"role": "user", "content": turn[1]})
        elif turn[0] == "agent":
            t.start(f"l{k}", "chat m", "llm", input=list(pending))
            t.end(f"l{k}", output={"text": turn[1]})
            pending = []
        else:
            _, name, args, res = turn
            t.start(f"t{k}", f"execute_tool {name}", "tool", input=args, attrs={"gen_ai.tool.name": name})
            if isinstance(res, Exception):
                t.end(f"t{k}", error=str(res))
            else:
                t.end(f"t{k}", output=res)
    t.finish(output=turns[-1][1], error=fail)
    return t.trace_id


def test_fault_user_details_match_no_account(mem):
    tid = _support_run(mem, [
        ("user", "Hi, cancel my order please. My email is sam@nowhere.com"),
        ("agent", "Let me find your account."),
        ("tool", "find_user_id_by_email", {"email": "sam@nowhere.com"}, RuntimeError("user not found")),
        ("agent", "I couldn't find that email. Could you check it?"),
        ("user", "Try sam@elsewhere.com"),
        ("agent", "Checking."),
        ("tool", "find_user_id_by_email", {"email": "sam@elsewhere.com"}, RuntimeError("user not found")),
        ("agent", "I'm sorry, I still can't find an account, so I can't make changes."),
    ])
    rep = analyze(mem.trace(tid), mem.blobs, judge=None)
    assert rep["fault"]["verdict"] == "user" and "matched no account" in rep["fault"]["reason"]
    assert "fault:      the user, not the agent" in rep["stack_trace"]


def test_fault_agent_when_the_agent_acted(mem):
    tid = _support_run(mem, [
        ("user", "Cancel order #2 please, my email is ann@x.com"),
        ("agent", "Looking you up."),
        ("tool", "find_user_id_by_email", {"email": "ann@x.com"}, {"user_id": "ann"}),
        ("agent", "Cancelling order #1."),
        ("tool", "cancel_order", {"order_id": "#1"}, {"ok": True}),
        ("agent", "Order #1 is cancelled."),
    ])
    assert analyze(mem.trace(tid), mem.blobs, judge=None)["fault"]["verdict"] == "agent"


def test_fault_decline_is_only_a_hint(mem):
    tid = _support_run(mem, [
        ("user", "My order is late."),
        ("agent", "I can cancel it for you. Would you like me to proceed?"),
        ("user", "No, I'll keep it and wait."),
        ("agent", "Understood, the order stays as it is."),
    ])
    f = analyze(mem.trace(tid), mem.blobs, judge=None)["fault"]
    assert f["verdict"] != "user" and any("declined" in h for h in f["hints"])


def test_fault_unfinished_when_waiting_on_the_user(mem):
    tid = _support_run(mem, [
        ("user", "I want to change my flight."),
        ("agent", "Sure. Which date would you like to fly instead?"),
    ])
    assert analyze(mem.trace(tid), mem.blobs, judge=None)["fault"]["verdict"] == "unfinished"


def test_fault_llm_judge_overrides_weak_rules(mem):
    tid = _support_run(mem, [
        ("user", "Exchange my earbuds for the cheapest pair."),
        ("agent", "Here are the options: A ($99), B ($79). Which one?"),
        ("user", "A please, that's the one I want."),
        ("agent", "Exchanging for A."),
        ("tool", "cancel_order", {"order_id": "#9"}, {"ok": True}),
        ("agent", "Done."),
    ])
    calls = []

    def judge(prompt):
        calls.append(prompt)
        if "who caused the failure" in prompt:
            return '{"fault": "user", "confidence": 0.8, "explanation": "the user picked A themselves"}'
        return "{}"
    f = analyze(mem.trace(tid), mem.blobs, judge=judge)["fault"]
    assert f["verdict"] == "user" and f["source"] == "llm_judge" and f["rules"]["verdict"] == "agent"
    assert any("TOOL cancel_order" in p for p in calls)


def test_runs_list_carries_fault_verdict_and_facets_list_filters(env):
    client, mem, _, app = env
    tdn.init(project="demo", exporter=mem)
    with tdn.run("FacetBot", input="hi") as run:
        tdn.tool_call("t", {"a": 1}, lambda a: a)
        run.fail("customer: wrong answer")
    push(client, mem)
    row = next(r for r in client.get("/api/v1/runs").json()["runs"] if r["trace_id"] == run.trace_id)
    assert row["fault"] and row["fault"]["verdict"] in ("agent", "user", "unfinished", "unclear")
    f = client.get("/api/v1/facets").json()
    assert "demo" in f["projects"] and "FacetBot" in f["agents"]
    assert client.get("/api/v1/facets?project=nope").json()["agents"] == []
