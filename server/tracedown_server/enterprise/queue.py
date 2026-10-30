"""Ingest queue: decouple accepting spans from processing them.

Without a queue (dev profile) the API runs the pipeline inline. With one, the API
writes payload blobs straight to storage (they are content-addressed, so writes are
idempotent), publishes span batches to the ``tdn.spans`` topic and returns 202; workers
(``tracedown-worker``) consume and run the same ``Pipeline``.

Messages are keyed by ``trace_id`` so every span of a run lands on one partition, in
order, and the root span (sent last by the SDK) arrives after its children.

* ``InProcessQueue``: a background thread; for single-node installs and tests.
* ``KafkaQueue``: Kafka or Redpanda through ``confluent-kafka`` (optional dependency).
  Offsets are committed only after the pipeline succeeds; a failed batch is rewound
  (``seek``) and retried with backoff, so nothing is skipped (at-least-once). Span writes
  are upserts, so redelivery is safe.
"""
from __future__ import annotations

import json
import queue as _queue
import threading
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

Handler = Callable[[str, List[Dict[str, Any]]], Any]
TOPIC = "tdn.spans"


def _split_by_trace(spans: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for s in spans:
        groups[s["trace_id"]].append(s)
    return groups


class InProcessQueue:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self._q: "_queue.Queue[Optional[tuple]]" = _queue.Queue()
        self.errors: List[str] = []
        self._t = threading.Thread(target=self._loop, name="tdn-ingest-worker", daemon=True)
        self._t.start()

    def publish(self, project: str, spans: List[Dict[str, Any]]) -> int:
        for _, batch in _split_by_trace(spans).items():
            self._q.put((project, batch))
        return len(spans)

    def _loop(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            try:
                self.handler(*item)
            except Exception as exc:  # noqa: BLE001 - keep the worker alive
                self.errors.append(f"{type(exc).__name__}: {exc}")
            finally:
                self._q.task_done()

    def flush(self, timeout: float = 10) -> None:
        self._q.join()

    def close(self) -> None:
        self._q.put(None)


class KafkaQueue:
    """Producer side (used by the API)."""

    def __init__(self, bootstrap: str, topic: str = TOPIC, producer=None, **conf: Any) -> None:
        if producer is None:
            from confluent_kafka import Producer  # optional dependency
            producer = Producer({"bootstrap.servers": bootstrap, "enable.idempotence": True,
                                 "compression.type": "zstd", "linger.ms": 20, **conf})
        self.producer, self.topic = producer, topic

    def publish(self, project: str, spans: List[Dict[str, Any]]) -> int:
        for trace_id, batch in _split_by_trace(spans).items():
            self.producer.produce(self.topic, key=trace_id.encode(),
                                  value=json.dumps({"project": project, "spans": batch}, default=str).encode())
        self.producer.poll(0)
        return len(spans)

    def flush(self, timeout: float = 10) -> None:
        self.producer.flush(timeout)


class KafkaWorker:
    """Consumer side: ``tracedown-worker --bootstrap redpanda:9092``."""

    def __init__(self, bootstrap: str, handler: Handler, group: str = "tdn-pipeline", topic: str = TOPIC,
                 consumer=None, **conf: Any) -> None:
        if consumer is None:
            from confluent_kafka import Consumer  # optional dependency
            consumer = Consumer({"bootstrap.servers": bootstrap, "group.id": group,
                                 "enable.auto.commit": False, "auto.offset.reset": "earliest", **conf})
        consumer.subscribe([topic])
        self.consumer, self.handler = consumer, handler
        self.processed = 0
        self.failed = 0

    def poll_once(self, timeout: float = 1.0) -> bool:
        msg = self.consumer.poll(timeout)
        if msg is None:
            return False
        if msg.error():
            self.failed += 1
            return True
        try:
            body = json.loads(msg.value())
            self.handler(body["project"], body["spans"])
        except Exception:  # noqa: BLE001
            self.failed += 1
            # Rewind to this message so the next poll redelivers it. Without the seek, a later
            # commit on the same partition would move past it and it would be lost.
            self._rewind(msg)
            raise
        self.consumer.commit(message=msg, asynchronous=False)
        self.processed += 1
        return True

    def _rewind(self, msg) -> None:
        try:
            from confluent_kafka import TopicPartition
            tp = TopicPartition(msg.topic(), msg.partition(), msg.offset())
        except ImportError:  # test doubles
            tp = msg
        self.consumer.seek(tp)

    def run_forever(self, max_backoff_s: float = 30.0) -> None:  # pragma: no cover - long-running loop
        import time
        backoff = 0.5
        try:
            while True:
                try:
                    self.poll_once()
                    backoff = 0.5
                except Exception as exc:  # noqa: BLE001
                    print(f"[tracedown-worker] batch failed, retrying in {backoff:.1f}s: {exc}")
                    time.sleep(backoff)  # a poison message blocks its partition; it shows in the logs
                    backoff = min(backoff * 2, max_backoff_s)
        finally:
            self.consumer.close()


def main() -> None:  # pragma: no cover - CLI entry point
    import argparse
    import os

    from ..pipeline import Pipeline
    from ..storage import SQLiteStore

    p = argparse.ArgumentParser(prog="tracedown-worker", description="Consume tdn.spans and run the pipeline")
    p.add_argument("--bootstrap", default=os.environ.get("TDN_KAFKA_BOOTSTRAP", "localhost:9092"))
    p.add_argument("--group", default="tdn-pipeline")
    p.add_argument("--db", default=os.environ.get("TDN_DB", "agent_tracedown.db"))
    args = p.parse_args()
    from . import storage_from_env
    store = storage_from_env(args.db) or SQLiteStore(args.db)
    pipe = Pipeline(store)
    print(f"tracedown-worker consuming {TOPIC} from {args.bootstrap}")
    KafkaWorker(args.bootstrap, pipe.process_spans, group=args.group).run_forever()
