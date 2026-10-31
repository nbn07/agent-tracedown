"""Load test: N spans into a ClickHouse span store, then query latency.

    PYTHONPATH=server:sdk/python python server/tests/integration/load_clickhouse.py \
        --url http://127.0.0.1:18123 --spans 1000000

Prints insert throughput and get-trace / tool-reliability latency percentiles.
"""
import argparse
import random
import statistics
import time
import uuid

from tracedown_server.enterprise.clickhouse import ClickHouseSpans, HttpClient

TOOLS = ["search", "lookup_order", "refund_order", "send_email", "get_weather", "book_flight", "sql", "browse"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--spans", type=int, default=1_000_000)
    ap.add_argument("--per-trace", type=int, default=20)
    ap.add_argument("--batch", type=int, default=20_000)
    ap.add_argument("--projects", type=int, default=10)
    a = ap.parse_args()
    db = "load_" + uuid.uuid4().hex[:6]
    store = ClickHouseSpans(HttpClient(a.url, timeout=300), database=db)
    rng = random.Random(1)
    traces, batch, t0, inserted = [], [], time.perf_counter(), 0
    base = time.time_ns() - 3600 * 10**9
    n_traces = a.spans // a.per_trace
    for t in range(n_traces):
        tid = uuid.UUID(int=rng.getrandbits(128)).hex
        traces.append(tid)
        start = base + t * 1_000_000
        project = f"proj-{t % a.projects}"
        for i in range(a.per_trace):
            dur = int(rng.lognormvariate(16, 1))  # ~9ms median
            batch.append({"trace_id": tid, "span_id": f"{t:08x}{i:08x}", "parent_span_id": None,
                          "name": "execute_tool", "kind": "tool" if i else "agent",
                          "start_time": start + i * 10**7, "end_time": start + i * 10**7 + dur,
                          "status": {"code": "ERROR" if rng.random() < 0.03 else "OK", "message": ""},
                          "attributes": {"tdn.step": i, "gen_ai.tool.name": rng.choice(TOOLS),
                                         "gen_ai.usage.input_tokens": rng.randint(50, 2000)}})
            if len(batch) >= a.batch:
                inserted += store.insert(project, batch)
                batch = []
        if t and t % (n_traces // 10) == 0:
            print(f"  {inserted:>9,} spans  {inserted / (time.perf_counter() - t0):,.0f}/s", flush=True)
    if batch:
        inserted += store.insert("proj-0", batch)
    ins_s = time.perf_counter() - t0
    print(f"inserted {inserted:,} spans in {ins_s:.1f}s = {inserted / ins_s:,.0f} spans/s (batches of {a.batch:,})")

    def timed(fn, n):
        out = []
        for _ in range(n):
            s = time.perf_counter()
            fn()
            out.append((time.perf_counter() - s) * 1000)
        out.sort()
        return f"p50 {statistics.median(out):.1f} ms, p95 {out[int(len(out) * 0.95) - 1]:.1f} ms"

    print("get one trace (20 spans):  ", timed(lambda: store.get(rng.choice(traces)), 200))
    print("tool reliability, 1 project:", timed(lambda: store.tool_reliability(f"proj-{rng.randrange(a.projects)}"), 20))
    print("tool reliability, all data: ", timed(lambda: store.tool_reliability(None), 10))


if __name__ == "__main__":
    main()
