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

import json
import os
from typing import Optional


def storage_from_env(db_path: str):
    """``TDN_CLICKHOUSE`` = JSON router config, or a single URL, switches spans to ClickHouse."""
    cfg = os.environ.get("TDN_CLICKHOUSE")
    if not cfg:
        return None
    from .clickhouse import ClusterRouter, HybridStore
    config = json.loads(cfg) if cfg.strip().startswith("{") else \
        {"clusters": {"default": {"url": cfg, "cluster": os.environ.get("TDN_CLICKHOUSE_CLUSTER") or None}}}
    return HybridStore(db_path, ClusterRouter(config))


def queue_from_env(handler) -> Optional[object]:
    """``TDN_KAFKA_BOOTSTRAP`` routes ingest through Kafka/Redpanda; ``TDN_QUEUE=inprocess`` uses a thread."""
    from .queue import InProcessQueue, KafkaQueue
    if os.environ.get("TDN_KAFKA_BOOTSTRAP"):
        return KafkaQueue(os.environ["TDN_KAFKA_BOOTSTRAP"])
    if os.environ.get("TDN_QUEUE") == "inprocess":
        return InProcessQueue(handler)
    return None
