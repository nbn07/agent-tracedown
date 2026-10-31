"""Scale and enterprise features (v2.0).

* ``queue``: Kafka/Redpanda ingest and workers
* ``clickhouse``: ClickHouse span storage, multi-cluster routing
* ``rbac``: users, roles, API keys, sessions
* ``sso``: OpenID Connect single sign-on
* ``audit``: hash-chained audit log
* ``pii``: server-side PII policies

Which of these are open source and which are commercial is decided separately (the
open-core boundary); the code is organized so that boundary can be drawn at this
package.
"""
from __future__ import annotations

import os
from typing import Optional


def storage_from_env(db_path: str):
    """Pick the store from the environment; ``None`` means the default SQLite file at ``db_path``.

    * ``TDN_DATABASE_URL=postgresql://...``: metadata (runs, findings, payloads, users, incidents...)
      in Postgres (``TDN_DATABASE_POOL`` connections per process, default 10).
    * ``TDN_CLICKHOUSE`` = JSON router config, or a single URL: spans in ClickHouse; metadata stays
      in Postgres when ``TDN_DATABASE_URL`` is set, else in SQLite.
    """
    url = os.environ.get("TDN_DATABASE_URL")
    if not url and not os.environ.get("TDN_CLICKHOUSE"):
        return None
    if url:
        from ..storage.postgres import PostgresStore
        store = PostgresStore(url, max_size=int(os.environ.get("TDN_DATABASE_POOL", "10")))
    else:
        from ..storage import SQLiteStore
        store = SQLiteStore(db_path)
    if os.environ.get("TDN_CLICKHOUSE"):
        from .clickhouse import router_from_env, with_clickhouse_spans
        store = with_clickhouse_spans(store, router_from_env())
    return store


def postgres_hybrid(url: str, router, **pool):
    """Postgres metadata + ClickHouse spans."""
    from ..storage.postgres import PostgresStore
    from .clickhouse import with_clickhouse_spans
    return with_clickhouse_spans(PostgresStore(url, **pool), router)


def queue_from_env(handler) -> Optional[object]:
    """``TDN_KAFKA_BOOTSTRAP`` routes ingest through Kafka/Redpanda; ``TDN_QUEUE=inprocess`` uses a thread."""
    from .queue import InProcessQueue, KafkaQueue
    if os.environ.get("TDN_KAFKA_BOOTSTRAP"):
        return KafkaQueue(os.environ["TDN_KAFKA_BOOTSTRAP"])
    if os.environ.get("TDN_QUEUE") == "inprocess":
        return InProcessQueue(handler)
    return None
