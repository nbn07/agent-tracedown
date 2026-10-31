"""POST /api/v1/import diagnoses a pasted export; read-only mode refuses writes."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from tracedown_server.api import create_app
from tracedown_server.importers import UnknownFormat, import_any, parse_text
from tracedown_server.storage import SQLiteStore

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
SAMPLES = os.path.join(ROOT, "examples", "imports")


@pytest.fixture()
def client():
    return TestClient(create_app(SQLiteStore(":memory:")))


def sample(name):
    return open(os.path.join(SAMPLES, name), encoding="utf-8").read()


def test_detects_each_format():
    assert import_any(parse_text(sample("langfuse-trace.json")))[0] == "langfuse"
    assert import_any(parse_text(sample("phoenix-otlp.json")))[0] == "otlp"
    otlp_lines = "\n".join(json.dumps(json.loads(sample("phoenix-otlp.json"))) for _ in range(2))
    assert import_any(parse_text(otlp_lines))[0] == "otlp"
    with pytest.raises(UnknownFormat):
        import_any(parse_text('{"hello": 1}'))
    with pytest.raises(UnknownFormat):
        parse_text("not json at all")


def test_import_returns_the_diagnosis(client):
    r = client.post("/api/v1/import", content=sample("langfuse-trace.json"))
    assert r.status_code == 200
    body = r.json()
    assert body["format"] == "langfuse" and len(body["runs"]) == 1
    run = body["runs"][0]
    assert run["root_cause"] and run["step"] is not None
    assert client.get(f"/api/v1/runs/{run['trace_id']}").status_code == 200


def test_tracedown_bundle_round_trip(client):
    tid = client.post("/api/v1/import", content=sample("langfuse-trace.json")).json()["runs"][0]["trace_id"]
    bundle = client.get(f"/api/v1/runs/{tid}/bundle")
    assert bundle.status_code == 200
    r = client.post("/api/v1/import?project=again", content=bundle.text)
    # the same trace id cannot move to another project
    assert r.status_code == 409


def test_import_errors_are_readable(client):
    r = client.post("/api/v1/import", content='{"hello": 1}')
    assert r.status_code == 422 and "Langfuse" in r.json()["detail"]
    assert client.post("/api/v1/import", content="").status_code == 422


def test_read_only_refuses_writes_but_allows_reading_and_analysis(client):
    tid = client.post("/api/v1/import", content=sample("langfuse-trace.json")).json()["runs"][0]["trace_id"]
    client.app.state.read_only = True
    assert client.get("/healthz").json()["read_only"] is True
    r = client.post("/api/v1/import", content=sample("phoenix-otlp.json"))
    assert r.status_code == 403 and "tracedown up" in r.json()["detail"]
    assert client.post("/v1/ingest/spans", json={"project": "x", "spans": []}).status_code == 403
    assert client.get(f"/api/v1/runs/{tid}").status_code == 200
    assert client.post(f"/api/v1/runs/{tid}/analyze").status_code == 200


def test_locked_database_gives_a_readable_error(client, monkeypatch):
    import sqlite3

    def locked(*a, **k):
        raise sqlite3.OperationalError("attempt to write a readonly database")
    monkeypatch.setattr(client.app.state.store, "put_blobs", locked)
    r = client.post("/api/v1/import", content=sample("langfuse-trace.json"))
    assert r.status_code == 503 and "OneDrive" in r.json()["detail"]


def test_unexpected_errors_are_json(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaput")
    monkeypatch.setattr(client.app.state.pipeline, "process_spans", boom)
    c = TestClient(client.app, raise_server_exceptions=False)
    r = c.post("/api/v1/import", content=sample("langfuse-trace.json"))
    assert r.status_code == 500 and "RuntimeError: kaput" in r.json()["detail"]


def test_synced_folders_are_recognized():
    from tracedown_server import cli
    assert cli.synced_folder("/Users/n/Library/CloudStorage/OneDrive-Personal/x/tracedown.db")
    assert cli.synced_folder("/Users/n/Dropbox/t.db")
    assert not cli.synced_folder("/Users/n/.tracedown/tracedown.db")
    assert cli.default_db().endswith("/.tracedown/tracedown.db")
