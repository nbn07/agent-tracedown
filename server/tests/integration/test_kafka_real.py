"""The Kafka ingest path against a real broker (Apache Kafka, Redpanda, or any broker that
speaks the Kafka protocol).

    TDN_IT_KAFKA_BOOTSTRAP=127.0.0.1:9092 pytest server/tests/integration/test_kafka_real.py

Each test uses its own topic and consumer group, so the broker can be shared.
"""
import json
import os
import threading
import time
import uuid
from collections import Counter, defaultdict

import pytest

BOOT = os.environ.get("TDN_IT_KAFKA_BOOTSTRAP")
pytestmark = pytest.mark.skipif(not BOOT, reason="set TDN_IT_KAFKA_BOOTSTRAP to a Kafka-protocol broker")


def _topic(partitions=6):
    from confluent_kafka.admin import AdminClient, NewTopic
    name = "it.spans." + uuid.uuid4().hex[:8]
    admin = AdminClient({"bootstrap.servers": BOOT})
    for f in admin.create_topics([NewTopic(name, num_partitions=partitions, replication_factor=1)]).values():
        f.result(30)
    deadline = time.time() + 30
    while time.time() < deadline:  # wait until every partition has a leader
        md = admin.list_topics(name, timeout=10).topics.get(name)
        if md and len(md.partitions) == partitions and all(p.leader >= 0 for p in md.partitions.values()):
            return name
        time.sleep(0.2)
    raise RuntimeError("topic not ready")


def _run(trace, n=5):
    t0 = time.time_ns()
    kids = [{"trace_id": trace, "span_id": f"{trace[:8]}{i:08x}", "parent_span_id": f"{trace[:8]}{0:08x}",
             "name": "execute_tool search", "kind": "tool", "start_time": t0 + i, "end_time": t0 + i + 1,
             "status": {"code": "OK", "message": ""}, "attributes": {"tdn.step": i, "gen_ai.tool.name": "search"},
             "input_digest": None, "output_digest": None} for i in range(1, n)]
    root = {"trace_id": trace, "span_id": f"{trace[:8]}{0:08x}", "parent_span_id": None, "name": "invoke_agent Bot",
            "kind": "agent", "start_time": t0, "end_time": t0 + n + 1, "status": {"code": "OK", "message": ""},
            "attributes": {"tdn.step": 0, "gen_ai.agent.name": "Bot", "tdn.run.root": True},
            "input_digest": None, "output_digest": None}
    return kids + [root]


class Recorder:
    """A thread-safe handler that records every delivery (stands in for the pipeline)."""

    def __init__(self, fail_first=0):
        self.lock = threading.Lock()
        self.deliveries = Counter()  # trace_id -> times handled
        self.spans = defaultdict(list)
        self.fail_left = fail_first

    def __call__(self, project, spans):
        with self.lock:
            if self.fail_left > 0:
                self.fail_left -= 1
                raise RuntimeError("database briefly unavailable")
            for s in spans:
                self.deliveries[s["trace_id"]] += 1 if s["parent_span_id"] is None else 0
            self.spans[spans[0]["trace_id"]].extend(spans)


def _workers(topic, group, handler, n, stop, conf=None):
    from tracedown_server.enterprise.queue import KafkaWorker
    ws = [KafkaWorker(BOOT, handler, group=group, topic=topic, **(conf or {})) for _ in range(n)]

    def loop(w):
        w.assigned = False
        while not stop.is_set():
            try:
                w.poll_once(0.2)
            except Exception:  # noqa: BLE001 - failed batch: it was rewound, poll again
                time.sleep(0.05)
            # read the assignment on the consumer's own thread (librdkafka calls are not meant
            # to race with poll from another thread)
            w.assigned = bool(w.consumer.assignment())
        w.consumer.close()

    ts = [threading.Thread(target=loop, args=(w,), daemon=True) for w in ws]
    for t in ts:
        t.start()
    return ws, ts


def _wait(cond, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.1)
    return False


def test_consumer_group_processes_every_trace_once():
    from tracedown_server.enterprise.queue import KafkaQueue
    topic = _topic(6)
    traces = [uuid.uuid4().hex for _ in range(300)]
    rec, stop = Recorder(), threading.Event()
    ws, ts = _workers(topic, "g-" + uuid.uuid4().hex[:6], rec, 3, stop)
    # let the group settle so the partitions are spread before any data arrives
    assert _wait(lambda: all(getattr(w, "assigned", False) for w in ws), 60), "group never assigned every worker"
    q = KafkaQueue(BOOT, topic=topic)
    for t in traces:
        q.publish("p", _run(t))
    assert q.flush(30) == 0 and q.delivery_errors == 0 and q.delivered == 300
    assert _wait(lambda: len(rec.deliveries) == 300)
    time.sleep(1)  # anything extra would arrive now
    stop.set()
    [t.join(10) for t in ts]
    assert set(rec.deliveries.values()) == {1}, "every trace exactly once with no failures"
    assert all(len(rec.spans[t]) == 5 for t in traces)
    assert sum(1 for w in ws if w.processed) >= 2, "partitions were shared across the group"


def test_spans_of_one_trace_share_a_partition_and_keep_order():
    from confluent_kafka import Consumer
    from tracedown_server.enterprise.queue import KafkaQueue
    topic = _topic(6)
    q = KafkaQueue(BOOT, topic=topic)
    traces = [uuid.uuid4().hex for _ in range(50)]
    for t in traces:  # the SDK may send a run in several batches: children first, root last
        spans = _run(t)
        q.publish("p", spans[:2])
        q.publish("p", spans[2:])
    q.flush(30)
    c = Consumer({"bootstrap.servers": BOOT, "group.id": "inspect-" + uuid.uuid4().hex[:6],
                  "auto.offset.reset": "earliest", "enable.auto.commit": False})
    c.subscribe([topic])
    seen = defaultdict(list)
    deadline = time.time() + 30
    while sum(len(v) for v in seen.values()) < 100 and time.time() < deadline:
        m = c.poll(0.5)
        if m is None or m.error():
            continue
        body = json.loads(m.value())
        seen[m.key().decode()].append((m.partition(), m.offset(), body["spans"]))
    c.close()
    for t in traces:
        msgs = sorted(seen[t], key=lambda x: x[1])
        assert len({p for p, _, _ in msgs}) == 1, "one partition per trace"
        assert msgs[-1][2][-1]["parent_span_id"] is None, "root span arrives last"


def test_crashed_worker_loses_nothing():
    """A worker dies mid-stream without committing; another member of the group takes over its
    partitions and redelivers the uncommitted messages (at-least-once)."""
    from tracedown_server.enterprise.queue import KafkaQueue, KafkaWorker
    topic = _topic(3)
    traces = [uuid.uuid4().hex for _ in range(120)]
    q = KafkaQueue(BOOT, topic=topic)
    for t in traces:
        q.publish("p", _run(t))
    q.flush(30)
    group = "g-" + uuid.uuid4().hex[:6]
    rec = Recorder()
    # Worker A handles 40 messages, then "crashes" between handling and committing one more.
    a = KafkaWorker(BOOT, rec, group=group, topic=topic, **{"session.timeout.ms": 6000})
    handled = 0
    while handled < 40:
        handled += bool(a.poll_once(1.0))
    crash = {"done": False}

    def handle_then_die(project, spans):
        rec(project, spans)
        crash["done"] = True
        raise SystemExit("worker killed before commit")
    a.handler = handle_then_die
    while not crash["done"]:
        try:
            a.poll_once(1.0)
        except SystemExit:
            pass
    a.consumer.close()  # leaves the group with its last offset uncommitted
    stop = threading.Event()
    # A short session timeout bounds the takeover delay on brokers that wait for the old
    # member's session to expire instead of acting on its LeaveGroup.
    ws, ts = _workers(topic, group, rec, 1, stop, {"session.timeout.ms": 6000})
    assert _wait(lambda: len(rec.deliveries) == 120, 60)
    stop.set()
    [t.join(10) for t in ts]
    assert set(rec.deliveries) == set(traces), "no trace lost"
    assert sum(rec.deliveries.values()) == 121, "exactly the one uncommitted message was redelivered"


def test_failed_batch_is_retried_not_skipped():
    from tracedown_server.enterprise.queue import KafkaQueue
    topic = _topic(1)  # one partition: a skipped offset would be easy to miss
    traces = [uuid.uuid4().hex for _ in range(30)]
    q = KafkaQueue(BOOT, topic=topic)
    for t in traces:
        q.publish("p", _run(t))
    q.flush(30)
    rec, stop = Recorder(fail_first=5), threading.Event()
    ws, ts = _workers(topic, "g-" + uuid.uuid4().hex[:6], rec, 1, stop)
    assert _wait(lambda: len(rec.deliveries) == 30)
    stop.set()
    [t.join(10) for t in ts]
    assert ws[0].failed == 5 and set(rec.deliveries.values()) == {1}


def test_backlog_is_visible_as_consumer_lag_and_drains():
    from confluent_kafka import Consumer, TopicPartition
    from tracedown_server.enterprise.queue import KafkaQueue
    topic = _topic(3)
    q = KafkaQueue(BOOT, topic=topic, **{"queue.buffering.max.messages": 50})  # tiny buffer: backpressure
    for _ in range(600):
        q.publish("p", _run(uuid.uuid4().hex, n=2))
    assert q.flush(30) == 0 and q.delivery_errors == 0 and q.delivered == 600
    group = "g-" + uuid.uuid4().hex[:6]

    def lag():
        c = Consumer({"bootstrap.servers": BOOT, "group.id": group})
        parts = [TopicPartition(topic, p) for p in range(3)]
        committed = c.committed(parts, timeout=10)
        total = 0
        for tp in committed:
            lo, hi = c.get_watermark_offsets(TopicPartition(topic, tp.partition), timeout=10)
            total += hi - (tp.offset if tp.offset >= 0 else lo)
        c.close()
        return total

    backlog = lag()
    if backlog < 600:  # some Kafka-compatible brokers (e.g. Tansu 0.6) report wrong high watermarks
        pytest.skip(f"broker reports high watermarks totalling {backlog} for 600 messages")
    assert backlog == 600
    rec, stop = Recorder(), threading.Event()
    ws, ts = _workers(topic, group, rec, 2, stop)
    assert _wait(lambda: len(rec.deliveries) == 600)
    stop.set()
    [t.join(10) for t in ts]
    assert _wait(lambda: lag() == 0, 20)
