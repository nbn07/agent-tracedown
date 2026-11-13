"""The GitHub Action reporter: table formatting, regression comparison, and
the end-to-end collection flow against a stub server (no real GitHub calls —
the comment POST is captured by the same stub)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tracedown_server import github_action as ga

RUNS_OK = [{"agent_name": "checkout_agent", "outcome": "success", "root_cause": None},
           {"agent_name": "refund_agent", "outcome": "failure",
            "root_cause": {"label": "Tool failed", "step": 4}}]


def test_run_row_formats_result_and_root_cause():
    ok, bad = (ga.run_row(r) for r in RUNS_OK)
    assert ok == {"conversation": "checkout_agent", "result": "success", "detail": ""}
    assert bad["result"] == "failure" and bad["detail"] == "Tool failed (step 4)"


def test_a_failure_anywhere_makes_the_conversation_failed():
    rows = [ga.run_row(r) for r in RUNS_OK + [dict(RUNS_OK[0], outcome="failure")]]
    outcomes = ga.outcomes_by_conversation(rows)
    assert outcomes == {"checkout_agent": "failure", "refund_agent": "failure"}


def test_regressions_are_pass_before_fail_now_only():
    baseline = {"a": "success", "b": "failure", "c": "success"}
    current = {"a": "failure", "b": "failure", "d": "failure"}
    assert ga.find_regressions(baseline, current) == ["a"]  # b already failed; d is new


def test_comment_is_a_table_with_root_causes_and_a_regression_banner():
    rows = [ga.run_row(r) for r in RUNS_OK]
    body = ga.format_comment(rows, ["refund_agent"], [])
    assert "| Conversation | Result | Root cause |" in body
    assert "| checkout_agent | success | — |" in body
    assert "| refund_agent | failure | Tool failed (step 4) |" in body
    assert "Regression" in body and "refund_agent" in body


def test_comment_with_no_runs_explains_the_wiring_instead_of_an_empty_table():
    body = ga.format_comment([], [], [])
    assert "No runs were recorded" in body and "127.0.0.1:7070" in body


class _Stub(BaseHTTPRequestHandler):
    runs: list = []
    posts: list = []

    def log_message(self, *args):  # keep the test output clean
        pass

    def do_GET(self):
        if self.path.startswith("/api/v1/runs"):
            payload = json.dumps({"runs": type(self).runs}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).posts.append((self.path, self.headers.get("Authorization"), body))
        self.send_response(201)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


@pytest.fixture()
def stub():
    _Stub.runs, _Stub.posts = [], []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_main_collects_reports_and_writes_the_baseline(stub, tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    _Stub.runs = RUNS_OK
    out = tmp_path / "report.md"
    baseline = tmp_path / "baseline.json"
    code = ga.main(["--endpoint", stub, "--baseline", str(baseline),
                    "--output", str(out), "--no-comment"])
    assert code == 0
    assert "| refund_agent | failure | Tool failed (step 4) |" in out.read_text()
    assert json.loads(baseline.read_text()) == {"checkout_agent": "success",
                                                 "refund_agent": "failure"}


def test_main_fails_on_a_regression_and_only_then(stub, tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"checkout_agent": "success"}))
    _Stub.runs = [dict(RUNS_OK[0], outcome="failure",
                       root_cause={"label": "Tool failed", "step": 2})]
    assert ga.main(["--endpoint", stub, "--baseline", str(baseline), "--no-comment"]) == 1
    assert ga.main(["--endpoint", stub, "--baseline", str(baseline), "--no-comment",
                    "--no-fail-on-regression"]) == 0


def test_main_posts_the_comment_when_a_pr_context_exists(stub, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_API_URL", stub)
    _Stub.runs = RUNS_OK
    code = ga.main(["--endpoint", stub, "--baseline", str(tmp_path / "b.json"),
                    "--repo", "o/r", "--pr", "3"])
    assert code == 0
    assert len(_Stub.posts) == 1
    path, auth, body = _Stub.posts[0]
    assert path == "/repos/o/r/issues/3/comments" and auth == "Bearer test-token"
    assert "Tracedown report" in json.loads(body)["body"]


def test_main_reports_a_dead_server_without_a_traceback(tmp_path):
    assert ga.main(["--endpoint", "http://127.0.0.1:1", "--no-comment",
                    "--baseline", str(tmp_path / "b.json")]) == 2
