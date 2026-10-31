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


def ensure_topic(bootstrap: str, topic: str = TOPIC, partitions: Optional[int] = None,
                 replication: Optional[int] = None, timeout: float = 15.0) -> bool:
    """Create the span topic if it is missing; True when it exists afterwards.

    Production clusters often disable topic auto-creation, and a producer writing to a missing
    topic loses its messages after accepting them. Partitions: ``TDN_KAFKA_PARTITIONS``
    (default 12, the most workers that can share the load); replication:
    ``TDN_KAFKA_REPLICATION`` (default: the broker's default). Best effort: with no permission to
    create topics, it logs and leaves creation to the operator.
    """
    import os
    from confluent_kafka.admin import AdminClient, NewTopic
    partitions = partitions or int(os.environ.get("TDN_KAFKA_PARTITIONS", "12"))
    replication = replication or int(os.environ.get("TDN_KAFKA_REPLICATION", "-1"))
    admin = AdminClient({"bootstrap.servers": bootstrap})
    if topic in admin.list_topics(timeout=timeout).topics:
        return True
    for rf in dict.fromkeys([replication, 1]):  # some brokers do not accept -1 ("broker default")
        fut = admin.create_topics([NewTopic(topic, num_partitions=partitions, replication_factor=rf)])[topic]
        try:
            fut.result(timeout)
            print(f"[tracedown] created Kafka topic {topic} ({partitions} partitions)", flush=True)
            return True
        except Exception as exc:  # noqa: BLE001
            if "TOPIC_ALREADY_EXISTS" in str(exc) or "already exists" in str(exc).lower():
                return True
            err = exc
    print(f"[tracedown] could not create Kafka topic {topic}: {err}; create it before sending spans", flush=True)
    return False


class KafkaQueue:
    """Producer side (used by the API).

    Delivery is asynchronous; failures are counted in ``delivery_errors`` (and logged). When
    the client's local buffer is full (the broker is slow or down), ``publish`` waits for
    deliveries instead of dropping spans: backpressure reaches the API, not data loss.
    """

    def __init__(self, bootstrap: str, topic: str = TOPIC, producer=None, **conf: Any) -> None:
        if producer is None:
            from confluent_kafka import Producer  # optional dependency
            ensure_topic(bootstrap, topic)
            producer = Producer({"bootstrap.servers": bootstrap, "enable.idempotence": True,
                                 "compression.type": "zstd", "linger.ms": 20, **conf})
        self.producer, self.topic = producer, topic
        self.delivered = 0
        self.delivery_errors = 0

    def _on_delivery(self, err, msg) -> None:
        if err is not None:
            self.delivery_errors += 1
            print(f"[tracedown] span batch not delivered to {self.topic}: {err}")
        else:
            self.delivered += 1

    def publish(self, project: str, spans: List[Dict[str, Any]]) -> int:
        for trace_id, batch in _split_by_trace(spans).items():
            value = json.dumps({"project": project, "spans": batch}, default=str).encode()
            for attempt in range(50):
                try:
                    self.producer.produce(self.topic, key=trace_id.encode(), value=value, on_delivery=self._on_delivery)
                    break
                except BufferError:  # local queue full: wait for the broker to catch up
                    self.producer.poll(0.1)
                except TypeError:  # test doubles without delivery callbacks
                    self.producer.produce(self.topic, key=trace_id.encode(), value=value)
                    break
            else:
                raise BufferError("Kafka producer queue stayed full for 5s; is the broker reachable?")
        self.producer.poll(0)
        return len(spans)

    def flush(self, timeout: float = 10) -> int:
        """Wait for outstanding deliveries; returns how many are still pending."""
        return self.producer.flush(timeout) or 0


class KafkaWorker:
    """Consumer side: ``tracedown-worker --bootstrap redpanda:9092``."""

    def __init__(self, bootstrap: str, handler: Handler, group: str = "tdn-pipeline", topic: str = TOPIC,
                 consumer=None, **conf: Any) -> None:
        if consumer is None:
            from confluent_kafka import Consumer  # optional dependency
            ensure_topic(bootstrap, topic)
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
        except (ImportError, AttributeError):  # test doubles
            tp = msg
        self.consumer.seek(tp)

    def stop(self) -> None:
        """Ask ``run_forever`` to finish the current message and leave the group cleanly."""
        self._stopping = True

    def run_forever(self, max_backoff_s: float = 30.0) -> None:  # pragma: no cover - long-running loop
        import time
        self._stopping = False
        self.last_poll = time.time()
        backoff = 0.5
        try:
            while not self._stopping:
                try:
                    self.poll_once()
                    self.last_poll = time.time()
                    backoff = 0.5
                except Exception as exc:  # noqa: BLE001
                    self.last_poll = time.time()
                    print(f"[tracedown-worker] batch failed, retrying in {backoff:.1f}s: {exc}")
                    time.sleep(backoff)  # a poison message blocks its partition; it shows in the logs
                    backoff = min(backoff * 2, max_backoff_s)
        finally:
            self.consumer.close()  # commits nothing new; the group rebalances right away


def _serve_health(worker: "KafkaWorker", port: int, stale_after_s: float = 120.0) -> None:  # pragma: no cover
    """``GET /healthz`` for Kubernetes probes: 200 while the poll loop is alive."""
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            age = time.time() - getattr(worker, "last_poll", time.time())
            ok = self.path.startswith("/healthz") and age < stale_after_s
            body = json.dumps({"status": "ok" if ok else "stale", "processed": worker.processed,
                               "failed": worker.failed, "last_poll_s": round(age, 1)}).encode()
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True, name="tdn-worker-health").start()


def build_pipeline(store, bus=None):
    """The same pipeline the API builds: PII policies and incident notifications included."""
    import os

    from ..incidents import hooks
    from ..pipeline import Pipeline
    from .pii import PolicyEngine
    notifier = hooks.Notifier(store, base_url=os.environ.get("TDN_PUBLIC_URL", ""))
    return Pipeline(store, notifier=notifier, pii=PolicyEngine(store), bus=bus)


def main(argv: Optional[List[str]] = None) -> None:  # pragma: no cover - CLI entry point
    import argparse
    import os
    import signal

    p = argparse.ArgumentParser(prog="tracedown-worker", description="Consume tdn.spans and run the pipeline")
    p.add_argument("--bootstrap", default=os.environ.get("TDN_KAFKA_BOOTSTRAP", "localhost:9092"))
    p.add_argument("--group", default=os.environ.get("TDN_KAFKA_GROUP", "tdn-pipeline"))
    p.add_argument("--topic", default=os.environ.get("TDN_KAFKA_TOPIC", TOPIC))
    p.add_argument("--db", default=os.environ.get("TDN_DB", "agent_tracedown.db"),
                   help="SQLite metadata path when TDN_DATABASE_URL is not set")
    p.add_argument("--health-port", type=int, default=int(os.environ.get("TDN_WORKER_HEALTH_PORT", "0") or 0),
                   help="serve GET /healthz on this port (0 = off)")
    args = p.parse_args(argv)
    from ..cache import cache_from_env, wrap_store
    from .clickhouse import store_from_env
    # With TDN_REDIS_URL, the worker's writes invalidate the API replicas' read cache and its
    # analyses reach the live feed of every replica.
    cache = cache_from_env()
    store = wrap_store(store_from_env(args.db), cache)
    pipe = build_pipeline(store, bus=cache.event_bus() if cache else None)
    worker = KafkaWorker(args.bootstrap, pipe.process_spans, group=args.group, topic=args.topic)
    signal.signal(signal.SIGTERM, lambda *_: worker.stop())  # Kubernetes sends SIGTERM before killing
    if args.health_port:
        _serve_health(worker, args.health_port)
    print(f"tracedown-worker consuming {args.topic} from {args.bootstrap} (group {args.group}, "
          f"store {type(store).__name__})", flush=True)
    worker.run_forever()
