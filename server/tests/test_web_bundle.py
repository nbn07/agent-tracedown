"""The server serves the exported web app (one prerendered page per detail route), and
`tracedown` parses its commands."""
import pytest
from fastapi.testclient import TestClient

from tracedown_server import api as api_mod
from tracedown_server import cli
from tracedown_server.storage import SQLiteStore


@pytest.fixture()
def client(tmp_path, monkeypatch):
    web = tmp_path / "web"
    for rel, body in {"index.html": "home", "404.html": "missing", "runs/index.html": "runs",
                      "runs/_/index.html": "run detail", "runs/_/index.txt": "rsc payload",
                      "_next/static/app.js": "js"}.items():
        f = web / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    monkeypatch.setattr(api_mod, "WEB", web)
    monkeypatch.delenv("TDN_WEB_DIR", raising=False)
    return TestClient(api_mod.create_app(SQLiteStore(str(tmp_path / "t.db"))), follow_redirects=False)


def test_pages_and_detail_routes(client):
    assert client.get("/").text == "home"
    assert client.get("/runs/").text == "runs"
    assert client.get("/runs/abc123/").text == "run detail"            # any id -> the one prerendered page
    assert client.get("/runs/abc123/index.txt").text == "rsc payload"  # client-side navigation payload
    assert client.head("/runs/").status_code == 200


def test_redirects_keep_the_query(client):
    r = client.get("/diff?a=1&b=2")
    assert r.status_code == 308 and r.headers["location"] == "/diff/?a=1&b=2"


def test_caching_and_404s(client):
    assert "immutable" in client.get("/_next/static/app.js").headers["cache-control"]
    assert client.get("/runs/").headers["cache-control"] == "no-cache"
    r = client.get("/nowhere/")
    assert r.status_code == 404 and r.text == "missing"
    assert client.get("/api/v1/nowhere").status_code == 404
    assert client.get("/api/v1/nowhere").headers["content-type"].startswith("application/json")
    assert client.get("/runs/../../etc/passwd").status_code in (308, 404)


def test_api_still_wins(client):
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/api/v1/runs").status_code == 200


def test_cli_parses():
    with pytest.raises(SystemExit):
        cli.main([])
    with pytest.raises(SystemExit) as e:
        cli.main(["up", "--help"])
    assert e.value.code == 0
