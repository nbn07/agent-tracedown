"""End-to-end ingest throughput through Kafka: producer -> topic -> N `tracedown-worker`
processes -> Postgres metadata store (plus the full analysis pipeline per run).

    PYTHONPATH=server:sdk/python python server/tests/integration/kafka_throughput.py \
        --bootstrap 127.0.0.1:19092 --database-url postgresql://postgres@127.0.0.1:55440/tdnbench \
        --runs 2000 --workers 1 3

Each run has --spans-per-run spans (one root). The clock stops when every run is in the
store, i.e. every message has been consumed, analyzed and committed.
"""
import argparse
import os
import signal
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))


def make_run(trace: str, n: int):
    t0 = time.time_ns()
    root_id = f"{trace[:8]}{0:08x}"
    spans = []
    for i in range(1, n):
        kind = "llm" if i % 3 == 1 else "tool"
        spans.append({"trace_id": trace, "span_id": f"{trace[:8]}{i:08x}", "parent_span_id": root_id,
                      "name": "chat gpt-4o-mini" if kind == "llm" else "execute_tool search", "kind": kind,
                      "start_time": t0 + i * 1000, "end_time": t0 + i * 1000 + 500,
                      "status": {"code": "OK", "message": ""},
                      "attributes": {"tdn.step": i, **({"gen_ai.request.model": "gpt-4o-mini",
                                                        "gen_ai.usage.input_tokens": 300,
                                                        "gen_ai.usage.output_tokens": 40} if kind == "llm"
                                                       else {"gen_ai.tool.name": "search"})},
                      "input_digest": None, "output_digest": None})
    spans.append({"trace_id": trace, "span_id": root_id, "parent_span_id": None, "name": "invoke_agent Bench",
                  "kind": "agent", "start_time": t0, "end_time": t0 + n * 1000,
                  "status": {"code": "OK", "message": ""},
                  "attributes": {"tdn.step": 0, "gen_ai.agent.name": "Bench", "tdn.run.root": True},
                  "input_digest": None, "output_digest": None})
    return spans


def count_runs(url: str, agent: str = "Bench") -> int:
    import psycopg
    with psycopg.connect(url) as c:
        try:
            return c.execute("SELECT count(*) FROM runs WHERE agent_name = %s", (agent,)).fetchone()[0]
        except psycopg.errors.UndefinedTable:  # the first worker has not created the schema yet
            return 0


def reset(url: str) -> None:
    import psycopg
    with psycopg.connect(url, autocommit=True) as c:
        for t in ("spans", "runs", "findings"):
            try:
                c.execute(f"DELETE FROM {t}")
            except Exception:  # noqa: BLE001 - table may not exist yet
                pass


def trial(a, workers: int) -> None:
    from confluent_kafka.admin import AdminClient, NewTopic
    from tracedown_server.enterprise.queue import KafkaQueue
    topic = "bench.spans." + uuid.uuid4().hex[:6]
    admin = AdminClient({"bootstrap.servers": a.bootstrap})
    for f in admin.create_topics([NewTopic(topic, num_partitions=a.partitions, replication_factor=1)]).values():
        f.result(30)
    time.sleep(1)
    reset(a.database_url)
    q = KafkaQueue(a.bootstrap, topic=topic)
    t0 = time.perf_counter()
    for _ in range(a.runs):
        q.publish("bench", make_run(uuid.uuid4().hex, a.spans_per_run))
    q.flush(60)
    prod_s = time.perf_counter() - t0
    spans = a.runs * a.spans_per_run
    env = dict(os.environ, TDN_DATABASE_URL=a.database_url, PYTHONPATH=os.pathsep.join(
        [os.path.join(ROOT, "server"), os.path.join(ROOT, "sdk", "python")]))
    cmd = [sys.executable, "-c", "from tracedown_server.enterprise.queue import main; main()",
           "--bootstrap", a.bootstrap, "--topic", topic, "--group", "bench-" + uuid.uuid4().hex[:6]]
    t1 = time.perf_counter()
    procs = [subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(workers)]
    try:
        while count_runs(a.database_url) < a.runs:
            if time.perf_counter() - t1 > a.timeout:
                raise TimeoutError(f"only {count_runs(a.database_url)} of {a.runs} runs after {a.timeout}s")
            time.sleep(0.25)
        cons_s = time.perf_counter() - t1
    finally:
        for p in procs:
            p.send_signal(signal.SIGTERM)
        for p in procs:
            try:
                p.wait(20)
            except subprocess.TimeoutExpired:
                p.kill()
    print(f"workers={workers}  partitions={a.partitions}  runs={a.runs}  spans={spans:,}  "
          f"produce {spans / prod_s:,.0f} spans/s  |  consume+analyze+store {spans / cons_s:,.0f} spans/s "
          f"({a.runs / cons_s:,.1f} runs/s, {cons_s:.1f}s incl. worker start-up)", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bootstrap", required=True)
    ap.add_argument("--database-url", required=True)
    ap.add_argument("--runs", type=int, default=2000)
    ap.add_argument("--spans-per-run", type=int, default=10)
    ap.add_argument("--partitions", type=int, default=6)
    ap.add_argument("--workers", type=int, nargs="+", default=[1, 3])
    ap.add_argument("--timeout", type=float, default=900)
    a = ap.parse_args()
    for w in a.workers:
        trial(a, w)


if __name__ == "__main__":
    main()
