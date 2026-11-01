"""tracedown mcp: the tools on the demo runs, and one end-to-end session over stdio."""
import asyncio
import os
import socket
import sys
import threading
import time

import pytest
import uvicorn

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [os.path.join(ROOT, "sdk", "python")]

from tracedown_server import mcp_server as m  # noqa: E402
from tracedown_server.api import create_app  # noqa: E402
from tracedown_server.demo.runner import load  # noqa: E402
from tracedown_server.storage import SQLiteStore  # noqa: E402

mcp = pytest.importorskip("mcp")


@pytest.fixture(scope="module")
def demo():
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
    ids, _, replay_id = load(url, verbose=False)
    yield m.Client(url), ids, replay_id
    srv.should_exit = True
    t.join(5)


def test_list_failed_runs_names_each_root_cause(demo):
    c, ids, _ = demo
    text = m.list_failed_runs(c, limit=20)
    assert ids["calendar"] in text and "Bad LLM output at step 4" in text
    assert "fault: user" in text  # the order-status run was the customer's mistake
    assert "replay" not in text.lower().split("(newest first):")[1]  # replays are left out


def test_explain_run_gives_the_stack_trace_evidence_and_fault(demo):
    c, ids, _ = demo
    text = m.explain_run(c, ids["calendar"])
    assert "Top suspect: Bad LLM output at step 4" in text
    assert "evidence: step 4 output 'Friday'" in text and "Who was at fault: agent" in text
    assert "not a certainty" in text


def test_get_step_shows_the_bad_value(demo):
    c, ids, _ = demo
    text = m.get_step(c, ids["calendar"], 4)
    assert "Friday" in text and "(llm)" in text
    assert "has no step 99" in m.get_step(c, ids["calendar"], 99)


def test_suggest_fixes_reports_what_worked(demo):
    c, ids, _ = demo
    text = m.suggest_fixes(c, ids["calendar"])
    assert "[WORKS, 1 of 1 replays passed] Bad LLM output at step 4" in text and "ISO-8601" in text
    assert "tdn.try_fixes" in text
    assert "needs a code change" in m.suggest_fixes(c, ids["loop"])


def test_check_replay_and_claims(demo):
    c, ids, replay_id = demo
    assert m.check_replay(c, ids["calendar"], replay_id).startswith("Passes Tracedown's checks")
    claims = m.check_claims(c, ids["refund"])
    assert "[stale] You will receive a refund of $120." in claims and "refund-policy-2023.md" in claims


def test_incidents_and_pasted_traces(demo):
    c, _, _ = demo
    assert "calendar_create_event" in m.list_incidents(c) or "Friday" in m.list_incidents(c)
    sample = open(os.path.join(ROOT, "web", "public", "samples", "langfuse-trace.json")).read()
    text = m.diagnose_trace(c, sample)
    assert text.startswith("Imported 1 run(s) from a langfuse export") and "step" in text


def test_errors_are_sentences_not_tracebacks(demo):
    c, _, _ = demo
    with pytest.raises(m.TracedownError, match="404"):
        m.explain_run(c, "no-such-run")
    with pytest.raises(m.TracedownError, match="Cannot reach the Tracedown server"):
        m.list_failed_runs(m.Client("http://127.0.0.1:9", timeout=2))


def test_a_real_mcp_session_over_stdio(demo):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    c, ids, _ = demo
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "tracedown_server.cli", "mcp", "--endpoint", c.endpoint],
        env={**os.environ, "PYTHONPATH": os.pathsep.join([os.path.join(ROOT, "server"),
                                                          os.path.join(ROOT, "sdk", "python")])})

    async def session():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as s:
                info = await s.initialize()
                tools = sorted(t.name for t in (await s.list_tools()).tools)
                result = await s.call_tool("explain_run", {"trace_id": ids["refund"]})
                return info, tools, result.content[0].text

    info, tools, text = asyncio.run(session())
    assert info.serverInfo.name == "tracedown"
    assert tools == sorted(["list_failed_runs", "explain_run", "get_step", "suggest_fixes", "check_replay",
                            "check_claims", "list_incidents", "diagnose_trace"])
    assert "Outdated retrieval source at step 2" in text
