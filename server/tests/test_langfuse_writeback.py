"""Langfuse write-back: a fake Langfuse HTTP server (stdlib only, no network beyond
localhost) checks the score/comment payloads, dedupe, and failure handling."""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from tracedown_server.importers.cli import main as cli_main
from tracedown_server.importers.langfuse import LangfuseClient
from tracedown_server.importers.langfuse_writeback import post_diagnosis

OBS = [
    {"id": "g1", "traceId": "lf-wb-1", "type": "GENERATION", "name": "draft",
     "startTime": "2026-10-29T10:00:00Z", "endTime": "2026-10-29T10:00:01Z", "level": "DEFAULT",
     "input": "hi", "output": "hello"},
    {"id": "x1", "traceId": "lf-wb-1", "type": "TOOL", "name": "save",
     "startTime": "2026-10-29T10:00:02Z", "level": "ERROR", "statusMessage": "disk full"},
]


class FakeLangfuse:
    def __init__(self, fail_posts=False):
        self.scores, self.comments, self.auth = [], [], []
        state = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                state.auth.append(self.headers.get("Authorization"))
                u = urlparse(self.path)
                if u.path.startswith("/api/public/traces/"):
                    self._json(200, {"id": "lf-wb-1", "name": "SaveAgent", "observations": OBS})
                elif u.path == "/api/public/scores":
                    q = parse_qs(u.query)
                    data = [s for s in state.scores if s["traceId"] == q.get("traceId", [""])[0]
                            and (not q.get("name") or s["name"] == q["name"][0])]
                    self._json(200, {"data": data})
                else:
                    self._json(404, {"error": "nope"})

            def do_POST(self):
                state.auth.append(self.headers.get("Authorization"))
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                if fail_posts:
                    self._json(500, {"error": "boom"})
                elif self.path == "/api/public/scores":
                    state.scores.append(body)
                    self._json(200, {"id": "s1"})
                elif self.path == "/api/public/comments":
                    state.comments.append(body)
                    self._json(200, {"id": "c1"})
                else:
                    self._json(404, {"error": "nope"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()


@pytest.fixture()
def fake(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    f = FakeLangfuse()
    yield f
    f.stop()


def _run(fake, *extra):
    return cli_main(["langfuse", "--trace-id", "lf-wb-1", "--host", fake.url, *extra])


def test_write_back_posts_score_and_comment(fake, capsys):
    assert _run(fake, "--write-back") == 0
    out = capsys.readouterr().out
    assert "Planning failure" in out and "wrote 1 diagnosis" in out  # the diagnosis itself, plus the write-back note
    (score,) = fake.scores
    assert score["traceId"] == "lf-wb-1" and score["name"] == "tracedown.root_cause"
    assert score["dataType"] == "CATEGORICAL" and score["value"] == "planning_failure"
    (comment,) = fake.comments
    assert comment["objectType"] == "TRACE" and comment["objectId"] == "lf-wb-1"
    assert "Tracedown diagnosis: Planning failure (root cause at step 2)" in comment["content"]
    assert "Suggested fix:" in comment["content"]
    expected = "Basic " + base64.b64encode(b"pk-test:sk-test").decode()
    assert fake.auth and all(a == expected for a in fake.auth)


def test_write_back_dedupes_on_a_second_run(fake):
    assert _run(fake, "--write-back") == 0
    assert _run(fake, "--write-back") == 0
    assert len(fake.scores) == 1 and len(fake.comments) == 1


def test_write_back_is_off_by_default(fake):
    assert _run(fake) == 0
    assert fake.scores == [] and fake.comments == []


def test_write_back_via_env_setting(fake, monkeypatch, capsys):
    monkeypatch.setenv("TDN_LANGFUSE_WRITEBACK", "1")
    assert _run(fake) == 0
    capsys.readouterr()
    assert len(fake.scores) == 1


def test_write_back_failure_keeps_the_diagnosis(monkeypatch, capsys):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    f = FakeLangfuse(fail_posts=True)
    try:
        assert _run(f, "--write-back") == 0
    finally:
        f.stop()
    captured = capsys.readouterr()
    assert "Planning failure" in captured.out  # the local diagnosis is still printed
    assert "write-back failed" in captured.err


def test_post_diagnosis_skips_runs_without_a_root_cause():
    class Stub:
        def scores(self, trace_id, name=None):
            raise AssertionError("must not be called")

    assert post_diagnosis(Stub(), "t1", {"root_cause": None}) == "no root cause"


def test_client_round_trip_against_the_fake(fake):
    client = LangfuseClient(fake.url, "pk-test", "sk-test")
    report = {"root_cause": {"category": "tool_failure", "label": "Tool failure", "step": 2,
                             "title": "save() failed: disk full", "evidence": ["step 2 disk full"]}}
    assert post_diagnosis(client, "lf-wb-1", report) == "posted"
    assert post_diagnosis(client, "lf-wb-1", report) == "already posted"
    assert len(fake.scores) == 1 and len(fake.comments) == 1
