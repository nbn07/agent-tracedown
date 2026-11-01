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
    incidents = m.list_incidents(c)
    assert "open incidents, most severe first" in incidents and "Agent loop" in incidents
    assert "1 run," in incidents and "1 runs" not in incidents
    assert m.list_incidents(c, "resolved") == "No resolved incidents."
    assert "incidents, most severe first" in m.list_incidents(c, "all")
    sample = open(os.path.join(ROOT, "web", "public", "samples", "langfuse-trace.json")).read()
    text = m.diagnose_trace(c, sample)
    assert text.startswith("Imported 1 run from a Langfuse export") and "step" in text
    otel = open(os.path.join(ROOT, "web", "public", "samples", "otel-trace.json")).read()
    assert m.diagnose_trace(c, otel).startswith("Imported 1 run from an OpenTelemetry (OTLP) export")


def test_errors_are_sentences_not_tracebacks(demo):
    c, _, _ = demo
    with pytest.raises(m.TracedownError, match="404"):
        m.explain_run(c, "no-such-run")
    with pytest.raises(m.TracedownError, match="Cannot reach the Tracedown server"):
        m.list_failed_runs(m.Client("http://127.0.0.1:9", timeout=2))
    with pytest.raises(m.TracedownError, match="must be an http"):
        m.Client("tracedown.example.com")
    with pytest.raises(m.TracedownError, match="must be an http"):
        m.Client("http://[::1")


def test_run_ids_cannot_reach_other_endpoints(demo):
    c, ids, _ = demo
    for bad in (ids["calendar"] + "/claims", ids["calendar"] + "?x=1", ids["calendar"] + "#f", "../../healthz",
                "with space", "a\nb", "..", "50%"):
        with pytest.raises(m.TracedownError, match="is not a Tracedown run id"):
            m.explain_run(c, bad)
    with pytest.raises(m.TracedownError, match="404"):
        m.explain_run(c, "0123456789abcdef")  # a well-formed id that does not exist
    with pytest.raises(m.TracedownError, match="404"):
        m.explain_run(c, "lf-trace:ünï@2026")  # custom ids are quoted, not refused


def _stub(handler_body, status=200, headers=()):
    """A one-route HTTP server for odd replies."""
    import http.server
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append((self.headers.get("Host"), self.headers.get("Authorization")))
            self.send_response(status)
            for k, v in headers:
                self.send_header(k, v.format(port=srv.server_address[1]))
            self.end_headers()
            self.wfile.write(handler_body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def test_the_api_key_does_not_follow_a_redirect():
    target, seen = _stub(b'{"runs": []}')
    redirect, first = _stub(b"", status=302, headers=[("Location", "http://localhost:%d/api/v1/runs" % target.server_address[1])])
    m.list_failed_runs(m.Client(f"http://127.0.0.1:{redirect.server_address[1]}", api_key="tdn_secret"))
    assert first[0][1] == "Bearer tdn_secret"     # the server we were pointed at gets the key
    assert seen and seen[0][1] is None             # the host it redirected to does not
    target.shutdown(); redirect.shutdown()


def test_a_server_that_is_not_tracedown_gets_a_plain_sentence():
    srv, _ = _stub(b"<html>hello</html>")
    with pytest.raises(m.TracedownError, match="did not answer like a Tracedown server"):
        m.list_failed_runs(m.Client(f"http://127.0.0.1:{srv.server_address[1]}"))
    srv.shutdown()


def test_explain_run_falls_back_when_the_key_may_not_analyse(demo):
    c, ids, _ = demo

    class Viewer(m.Client):
        def get(self, path, **params):
            data = super().get(path, **params)
            if path.endswith(ids["refund"]):
                data["findings"] = None  # as if never analysed
            return data

        def post(self, path, body, **params):
            raise m.TracedownError("Tracedown answered 403: missing permission analysis:run")

    text = m.explain_run(Viewer(c.endpoint), ids["refund"])
    assert "has not been analysed" in text


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
                missing = await s.call_tool("explain_run", {"trace_id": "no-such-run"})
                listed = (await s.list_tools()).tools
                return info, tools, result.content[0].text, missing, listed

    info, tools, text, missing, listed = asyncio.run(session())
    assert missing.isError and "404" in missing.content[0].text  # an error, and a sentence
    hints = {t.name: t.annotations.readOnlyHint for t in listed}
    assert hints["explain_run"] is True and hints["diagnose_trace"] is False
    assert info.serverInfo.name == "tracedown"
    assert tools == sorted(["list_failed_runs", "explain_run", "get_step", "suggest_fixes", "check_replay",
                            "check_claims", "list_incidents", "diagnose_trace"])
    assert "Outdated retrieval source at step 2" in text


def test_a_missing_key_says_how_to_set_it():
    srv, _ = _stub(b'{"detail": "authentication required"}', status=401)
    with pytest.raises(m.TracedownError, match=r"authentication required\. Set TDN_API_KEY"):
        m.list_failed_runs(m.Client(f"http://127.0.0.1:{srv.server_address[1]}"))
    srv.shutdown()
