"""Proven fixes, part 1: fix-check and tdn.confirm_cause on the calendar demo."""
import json
import os
import socket
import sys
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [os.path.join(ROOT, "sdk", "python"), os.path.join(ROOT, "examples")]

import agent_tracedown as tdn  # noqa: E402
import demo_agents as demo  # noqa: E402
from tracedown_server import fixes  # noqa: E402
from tracedown_server.api import create_app  # noqa: E402
from tracedown_server.rca import analyze  # noqa: E402
from tracedown_server.storage import SQLiteStore  # noqa: E402


def _calendar(mem, extractor=demo.extract_event_buggy):
    try:
        demo.calendar_agent(extractor=extractor)
    except tdn.ToolError:
        pass
    tid = mem.spans[-1]["trace_id"]
    return analyze(mem.trace(tid), mem.blobs)


@pytest.fixture()
def mem():
    m = tdn.MemoryExporter()
    tdn.init(project="demo", exporter=m)
    return m


# ----------------------------------------------------------------------------- the check itself

def test_check_passes_when_the_blamed_problem_is_gone(mem):
    before, after = _calendar(mem), _calendar(mem, demo.extract_event_fixed)
    r = fixes.check(before, after)
    assert r["passed"] and r["verdict"] == "passes_tracedown_checks"
    assert r["checks"] == {"blamed_problem_gone": True, "no_new_problem": True, "finished": True, "user_test": None}
    assert "is gone" in r["reasons"][0]


def test_check_fails_when_the_same_problem_is_back(mem):
    before, again = _calendar(mem), _calendar(mem)
    r = fixes.check(before, again)
    assert not r["passed"] and r["verdict"] == "not_fixed"
    assert r["checks"]["blamed_problem_gone"] is False and "still there" in r["reasons"][0]


def test_users_own_test_decides_too(mem):
    before, after = _calendar(mem), _calendar(mem, demo.extract_event_fixed)
    assert fixes.check(before, after, user_passed=True)["verdict"] == "fixed"
    r = fixes.check(before, after, user_passed=False)
    assert not r["passed"] and "your test failed" in r["reasons"]


def test_new_problem_and_crash_fail_the_check():
    rc = {"category": "bad_llm_output", "label": "Bad LLM output", "step": 4, "title": "t"}
    original = {"root_cause": rc, "findings": [rc]}
    new = {"category": "loop", "label": "Loop", "step": 9, "title": "search called 4 times", "confidence": 0.9}
    crash = {"category": "unhandled_error", "label": "Error", "step": 1, "title": "KeyError", "confidence": 0.6}
    assert fixes.check(original, {"findings": [new]})["checks"]["no_new_problem"] is False
    assert fixes.check(original, {"findings": [crash]})["checks"]["finished"] is False
    assert fixes.check({"root_cause": None}, {})["verdict"] == "nothing_to_fix"
    assert fixes.summarize([{"passed": True}, {"passed": False}])["passed"] is False


def test_claim_problems_count_anywhere_in_the_replay():
    rc = {"category": "hallucination", "label": "Hallucination", "step": 3, "title": "t"}
    moved = {"category": "hallucination", "label": "Hallucination", "step": 5, "title": "'$49' unsupported",
             "confidence": 0.7}
    assert fixes.check({"root_cause": rc, "findings": [rc]}, {"findings": [moved]})["passed"] is False


# ----------------------------------------------------------------------------- API

def test_fix_check_endpoint(mem):
    client = TestClient(create_app(store=SQLiteStore(":memory:"), api_keys=[]))
    _calendar(mem)
    bad = mem.spans[-1]["trace_id"]
    bundle = {"spans": mem.trace(bad), "blobs": mem.blobs}
    with tdn.replay(bundle, mode="from-step", from_step=4,
                    overrides={"llm_fn": demo.extract_event_fixed, "steps": [4]}):
        demo.calendar_agent()
    rep = mem.spans[-1]["trace_id"]
    client.post("/v1/ingest/blobs", json={"project": "demo", "blobs": mem.blobs})
    client.post("/v1/ingest/spans", json={"project": "demo", "spans": mem.spans})
    r = client.get(f"/api/v1/runs/{bad}/fix-check", params={"replay": rep}).json()
    assert r["passed"] and r["root_cause"]["step"] == 4 and r["original"] == bad
    r = client.get(f"/api/v1/runs/{bad}/fix-check", params={"replay": rep, "user_test": "false"}).json()
    assert not r["passed"]
    assert client.get(f"/api/v1/runs/{bad}/fix-check", params={"replay": "nope"}).status_code == 404


# ----------------------------------------------------------------------------- tdn.confirm_cause, end to end

@pytest.fixture()
def server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(create_app(store=SQLiteStore(":memory:"), api_keys=[]),
                                        host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    while not srv.started:
        time.sleep(0.05)
    url = f"http://127.0.0.1:{port}"
    tdn.init(project="demo", endpoint=url)
    yield url
    srv.should_exit = True
    t.join(5)


def _bad_run(url):
    try:
        demo.calendar_agent()
    except tdn.ToolError:
        pass
    import urllib.request
    with urllib.request.urlopen(url + "/api/v1/runs?outcome=failure&limit=1") as r:
        return json.loads(r.read())["runs"][0]["trace_id"]


FIXED = json.dumps({"title": "Call with Priya", "start": "2026-11-06T15:00:00-05:00"})
STUB = {"calendar_create_event": demo.create_event}


def test_confirm_cause_confirms_the_blamed_step(server):
    tid = _bad_run(server)
    r = tdn.confirm_cause(tid, corrected=FIXED, agent=demo.calendar_agent, tools=STUB)
    assert r["verdict"] == "cause_confirmed" and r["step"] == 4 and len(r["tries"]) == 3
    assert all(t["checks"]["blamed_problem_gone"] for t in r["tries"])


def test_confirm_cause_rejects_a_correction_that_does_not_help(server):
    tid = _bad_run(server)
    still_wrong = json.dumps({"title": "Call with Priya", "start": "Friday"})
    r = tdn.confirm_cause(tid, corrected=still_wrong, agent=demo.calendar_agent, tools=STUB, tries=1)
    assert r["verdict"] == "not_confirmed" and not r["passed"]


def test_confirm_cause_never_runs_tools_for_real_by_default(server):
    tid = _bad_run(server)
    r = tdn.confirm_cause(tid, corrected=FIXED, agent=demo.calendar_agent)
    assert r["verdict"] == "blocked" and "calendar_create_event" in r["summary"]
    ok = tdn.confirm_cause(tid, corrected=FIXED, agent=demo.calendar_agent,
                           live_tools=["calendar_create_event"], tries=1)
    assert ok["verdict"] == "cause_confirmed"


def test_confirm_cause_runs_the_users_test(server):
    tid = _bad_run(server)
    r = tdn.confirm_cause(tid, corrected=FIXED, agent=demo.calendar_agent, tools=STUB, tries=1,
                          test=lambda trace_id: False)
    assert r["verdict"] == "not_confirmed" and "your test failed" in r["tries"][0]["reasons"]


# ----------------------------------------------------------------------------- part 2: suggested fixes

def _suggestions(url, tid):
    import urllib.request
    with urllib.request.urlopen(f"{url}/api/v1/runs/{tid}/suggestions") as r:
        return json.loads(r.read())["suggestions"]


AGENTS = {"refund": "SupportBot", "pricing": "SalesAssistant", "injection": "ProductResearcher",
          "loop": "ResearchAssistant", "order_status": "OrderStatusAgent"}


def _failed(url, story):
    """Run a demo story and return its trace id (some stories end 'successfully' but wrong)."""
    try:
        demo.ALL[story]()
    except Exception:  # noqa: BLE001 - the demo runs fail on purpose
        pass
    import urllib.request
    with urllib.request.urlopen(f"{url}/api/v1/runs?agent={AGENTS[story]}&limit=1") as r:
        return json.loads(r.read())["runs"][0]["trace_id"]


def test_suggestion_for_a_bad_date_is_a_prompt_instruction(server):
    tid = _bad_run(server)
    [s] = _suggestions(server, tid)
    assert s["category"] == "bad_llm_output" and s["step"] == 4
    assert "ISO-8601" in s["try"]["overrides"]["prompt_note"] and s["try"]["from_step"] == 4
    assert "calendar_create_event" in s["change"]


def test_try_fixes_proves_the_date_instruction_works(server):
    tid = _bad_run(server)
    [r] = tdn.try_fixes(tid, agent=demo.calendar_agent, tools=STUB)
    assert r["verdict"] == "works" and len(r["checks"]) == 3, r["summary"]


def test_try_fixes_leaves_out_the_outdated_policy(server):
    tid = _failed(server, "refund")
    [r] = tdn.try_fixes(tid, agent=demo.refund_agent, tries=1)
    assert r["category"] == "stale_source" and r["verdict"] == "works", r["summary"]
    assert "outdated" in r["tries_as"]


def test_try_fixes_grounds_a_made_up_price(server):
    tid = _failed(server, "pricing")
    [r] = tdn.try_fixes(tid, agent=demo.pricing_agent, tries=1, tools={"get_price": lambda plan: {
        "plan": "pro", "usd_per_month": 39}})
    assert r["category"] == "hallucination" and r["verdict"] == "works", r["summary"]


def test_try_fixes_drops_the_page_with_hidden_instructions(server):
    tid = _failed(server, "injection")
    [r] = tdn.try_fixes(tid, agent=demo.injection_agent, tries=1)
    assert r["category"] == "prompt_injection" and r["verdict"] == "works", r["summary"]


def test_without_the_fix_the_replay_stops_before_sending_email(server):
    tid = _failed(server, "injection")
    bundle = tdn.fetch_bundle(server, tid)
    with pytest.raises(tdn.SideEffectBlocked):
        with tdn.replay(bundle, mode="from-step", from_step=2, overrides={"live_tools": []}):
            demo.injection_agent()


def test_a_fix_that_fails_the_users_test_does_not_work(server):
    tid = _bad_run(server)
    [r] = tdn.try_fixes(tid, agent=demo.calendar_agent, tools=STUB, tries=1, test=lambda value: False)
    assert r["verdict"] == "does_not_work" and "your test failed" in r["summary"]


def test_code_changes_and_user_mistakes_are_not_tried(server):
    loop = tdn.try_fixes(_failed(server, "loop"), agent=demo.research_loop_agent)
    assert [r["verdict"] for r in loop] == ["not_tried"] and "stop condition" in loop[0]["change"]
    user = tdn.try_fixes(_failed(server, "order_status"), agent=demo.ALL["order_status"])
    assert user[0]["verdict"] == "not_tried" and "not the agent's mistake" in user[0]["change"]


# ----------------------------------------------------------------------------- part 3: results in the web app

def test_suggestions_show_whether_each_fix_worked(server):
    tid = _bad_run(server)
    [before] = _suggestions(server, tid)
    assert before["status"] == "untested" and before["replays"] == []
    tdn.try_fixes(tid, agent=demo.calendar_agent, tools=STUB, tries=2)
    import urllib.request
    with urllib.request.urlopen(f"{server}/api/v1/runs/{tid}/suggestions") as r:
        data = json.loads(r.read())
    [after] = data["suggestions"]
    assert after["status"] == "works" and after["result"] == "2 of 2 replays passed"
    assert all(rep["suggestion"] == "bad_llm_output@4" for rep in after["replays"])
    assert data["other_replays"] == []


def test_a_users_own_replay_is_listed_separately_and_checked():
    sugg = [{"key": "bad_llm_output@4", "try": {"from_step": 4}}, {"key": "loop@3", "try": None}]
    replays = [{"trace_id": "a", "suggestion": None, "check": {"passed": True}},
               {"trace_id": "b", "suggestion": "bad_llm_output@4", "check": {"passed": False}}]
    out = fixes.with_results(sugg, replays)
    assert [s["status"] for s in out["suggestions"]] == ["does_not_work", "not_tried"]
    assert [r["trace_id"] for r in out["other_replays"]] == ["a"]
