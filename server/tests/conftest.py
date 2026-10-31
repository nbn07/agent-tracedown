"""Run the whole server suite on another metadata store.

    TDN_TEST_DATABASE_URL=postgresql://tdn@127.0.0.1:55432/tdn pytest server/tests

With the variable set, every ``SQLiteStore(...)`` a test creates becomes a ``PostgresStore``
in a fresh, throwaway schema, so each test starts from an empty database exactly as it does
with ``:memory:`` SQLite. Without it, nothing changes.
"""
import os
import sys
import uuid

import pytest

URL = os.environ.get("TDN_TEST_DATABASE_URL")


@pytest.fixture(autouse=True)
def _store_backend(monkeypatch, request):
    if not URL:
        yield
        return
    import tracedown_server.enterprise.clickhouse  # noqa: F401 - subclasses the real SQLiteStore; import first
    import tracedown_server.storage as storage_mod
    from tracedown_server.storage.postgres import PostgresStore

    made, schemas = [], {}

    def factory(path=":memory:", *a, **kw):
        # ":memory:" is a fresh database every time; a file path is shared, like two replicas on one database
        schema = "t_" + uuid.uuid4().hex[:16] if path == ":memory:" else \
            schemas.setdefault(path, "t_" + uuid.uuid4().hex[:16])
        s = PostgresStore(URL, schema=schema, max_size=4)
        made.append(s)
        return s

    monkeypatch.setattr(storage_mod, "SQLiteStore", factory)
    for mod in list(sys.modules.values()):
        if getattr(mod, "__name__", "").startswith(("test_", "tests.")) and getattr(mod, "SQLiteStore", None) is not None:
            monkeypatch.setattr(mod, "SQLiteStore", factory)
    yield
    import psycopg
    for s in made:
        s.close()
    with psycopg.connect(URL, autocommit=True) as c:
        for schema in {s.schema for s in made}:
            c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.fixture
def chdb_client():
    """An embedded ClickHouse engine (chdb), closed when the test ends.

    Left open until the interpreter exits, chdb's background threads race Python's own
    shutdown and sometimes crash the test run with a segfault (exit code 139) after every
    test has passed. Seen on Python 3.10 in CI; about 1 run in 4 for the test alone."""
    pytest.importorskip("chdb")
    from tracedown_server.enterprise.clickhouse import ChdbClient
    client = ChdbClient()
    try:
        yield client
    finally:
        client.close()
