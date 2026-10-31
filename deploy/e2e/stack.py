"""Full-stack smoke test on one machine, no Docker.

Starts Postgres-backed metadata, Redis, a Kafka-protocol broker, two ClickHouse endpoints (routed
by project), two API processes, two Kafka workers and the Next.js web app; sends the demo's agent
runs through the SDK; then checks every layer and drives the web app in Chromium (Playwright).

    TDN_E2E_PG_ADMIN=postgresql://user@127.0.0.1:5432/postgres \
    TDN_E2E_KAFKA_CMD="kafka-server-start.sh config/kraft/server.properties" \
    python deploy/e2e/stack.py

Needs: a running Postgres (TDN_E2E_PG_ADMIN, a role that can create databases), redis-server on
PATH, a Kafka-compatible broker command listening on 127.0.0.1:59092 (TDN_E2E_KAFKA_CMD), chdb
(ClickHouse engine, pip install chdb), the web app built (cd web && npm run build) and Playwright.
Logs and screenshots go to deploy/e2e/out/. Exit status 1 if any check fails.
"""
import json, os, signal, subprocess, sys, time, urllib.request

R = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
S = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(S, exist_ok=True)
PG_ADMIN = os.environ.get("TDN_E2E_PG_ADMIN", "postgresql://tdn@127.0.0.1:55432/tdn")
PG = PG_ADMIN.rsplit("/", 1)[0] + "/tdn_e2e"
KAFKA_CMD = os.environ.get("TDN_E2E_KAFKA_CMD")
PORTS = dict(redis=56379, kafka=59092, ch1=58123, ch2=58124, api=57070, web=53000, w1=57101, w2=57102)
procs = []


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True, **kw).stdout


def start(name, cmd, env=None, cwd=None):
    log = open(f"{S}/{name}.log", "w")
    p = subprocess.Popen(cmd, shell=True, stdout=log, stderr=subprocess.STDOUT, env={**os.environ, **(env or {})},
                         cwd=cwd, preexec_fn=os.setsid)
    procs.append((name, p))
    return p


def wait_http(url, timeout=60, ok=lambda b: True):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                b = r.read()
                if ok(b):
                    return b
        except Exception:
            pass
        time.sleep(0.5)
    raise SystemExit(f"timeout waiting for {url}")


def get(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORTS['api']}{path}", timeout=10) as r:
        return json.loads(r.read())


def ch_count(port):
    q = urllib.parse.quote("SELECT count() FROM tdn.spans")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/?query={q}", timeout=10) as r:
        return int(r.read().decode().split()[0])


def cleanup(*_):
    for _, p in reversed(procs):
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except Exception:
            pass


import urllib.parse  # noqa: E402

try:
    if not KAFKA_CMD:
        raise SystemExit("set TDN_E2E_KAFKA_CMD to a command that starts a broker on 127.0.0.1:59092")
    sh(f"psql {PG_ADMIN} -c 'DROP DATABASE IF EXISTS tdn_e2e' -c 'CREATE DATABASE tdn_e2e'")
    start("redis", f"redis-server --port {PORTS['redis']} --save '' --appendonly no")
    start("kafka", KAFKA_CMD)
    for n in ("ch1", "ch2"):
        sh(f"rm -rf {S}/{n}-data")
        start(n, f"{sys.executable} {R}/server/tests/integration/chdb_http.py --port {PORTS[n]} --path {S}/{n}-data")
    for n in ("ch1", "ch2"):
        wait_http(f"http://127.0.0.1:{PORTS[n]}/ping")
    import socket
    for _ in range(60):
        try:
            socket.create_connection(("127.0.0.1", PORTS["kafka"]), 1).close(); break
        except OSError:
            time.sleep(0.5)
    else:
        raise SystemExit("broker did not start: " + open(f"{S}/kafka.log").read()[-400:])
    time.sleep(2)
    env = {
        "TDN_DATABASE_URL": PG,
        "TDN_REDIS_URL": f"redis://127.0.0.1:{PORTS['redis']}/0",
        "TDN_KAFKA_BOOTSTRAP": f"127.0.0.1:{PORTS['kafka']}",
        "TDN_CLICKHOUSE": json.dumps({"clusters": {"us": {"url": f"http://127.0.0.1:{PORTS['ch1']}"},
                                                   "eu": {"url": f"http://127.0.0.1:{PORTS['ch2']}"}},
                                      "routes": {"demo-eu": "eu"}, "default": "us"}),
        "PYTHONPATH": f"{R}/server:{R}/sdk/python",
    }
    start("api", f"tracedown-server --host 127.0.0.1 --port {PORTS['api']} --workers 2", env)
    wait_http(f"http://127.0.0.1:{PORTS['api']}/healthz", 90)
    for n in ("w1", "w2"):
        start(n, f"tracedown-worker --health-port {PORTS[n]}", env)
    for n in ("w1", "w2"):
        wait_http(f"http://127.0.0.1:{PORTS[n]}/healthz", 60)
    start("web", "node server.js", {"PORT": str(PORTS["web"]), "HOSTNAME": "127.0.0.1",
                                    "TDN_API_URL": f"http://127.0.0.1:{PORTS['api']}"}, cwd=f"{R}/web/.next/standalone")
    wait_http(f"http://127.0.0.1:{PORTS['web']}/runs", 60)
    print("stack up", flush=True)

    # real agent runs through the SDK -> API -> Kafka -> workers -> Postgres + ClickHouse
    out = sh(f"{sys.executable} {R}/examples/run_demo.py --endpoint http://127.0.0.1:{PORTS['api']} 2>&1 || true",
             env={**os.environ, **env})
    open(f"{S}/demo.log", "w").write(out)
    # a second project routed to the other ClickHouse cluster
    sh(f"""{sys.executable} -c "
import agent_tracedown as tdn
tdn.init(project='demo-eu', endpoint='http://127.0.0.1:{PORTS['api']}')
for i in range(5):
    with tdn.run('EuBot', input='hello') as run:
        tdn.tool_call('lookup', {{'q': i}}, lambda q: {{'ok': True}})
        if i % 2: run.fail('customer: wrong answer')
" """, env={**os.environ, **env})

    expected = 5 + 4 + 80 + 1 + 5  # demo agents, repeats, drift cohorts, replay, demo-eu
    end = time.time() + 120
    while time.time() < end:
        n = len(get("/api/v1/runs?limit=500")["runs"])
        if n >= expected - 2:
            break
        time.sleep(2)
    runs = get("/api/v1/runs?limit=500")["runs"]
    results = {
        "runs_seen": len(runs), "runs_expected_about": expected,
        "failed_runs_with_fault_verdict": sum(1 for r in runs if r["outcome"] == "failure" and r.get("fault")),
        "failed_runs": sum(1 for r in runs if r["outcome"] == "failure"),
        "projects": get("/api/v1/facets")["projects"],
        "clickhouse_us_spans": ch_count(PORTS["ch1"]), "clickhouse_eu_spans": ch_count(PORTS["ch2"]),
        "postgres_spans_table_rows": int(sh(f"psql {PG} -tAc 'SELECT count(*) FROM runs'").strip()),
        "incidents": len(get("/api/v1/incidents")["incidents"]),
        "drift_reports": len(get("/api/v1/drift/reports")["reports"]),
        "redis_keys": int(sh(f"redis-cli -p {PORTS['redis']} dbsize").strip()),
        "worker_health": [json.loads(wait_http(f"http://127.0.0.1:{PORTS[n]}/healthz")) for n in ("w1", "w2")],
    }
    cal = next(r for r in runs if r["agent_name"] == "CalendarAssistant" and r["outcome"] == "failure")
    results["calendar_root_cause"] = (cal.get("root_cause") or {}).get("label"), (cal.get("root_cause") or {}).get("step")
    results["calendar_trace"] = cal["trace_id"]
    json.dump(results, open(f"{S}/results.json", "w"), indent=1, default=str)
    print(json.dumps(results, indent=1, default=str), flush=True)

    # the web app, through its proxy, in a real browser
    sh(f"node {os.path.dirname(os.path.abspath(__file__))}/browse.js http://127.0.0.1:{PORTS['web']} {cal['trace_id']} {S}", cwd=S)
    browse = open(f"{S}/browse.log").read()
    print(browse, flush=True)
    bad = [k for k, ok in {
        "every run arrived": results["runs_seen"] >= results["runs_expected_about"] - 2,
        "every failed run has a fault verdict": results["failed_runs_with_fault_verdict"] == results["failed_runs"],
        "spans routed to both ClickHouse clusters": results["clickhouse_us_spans"] > 0 and results["clickhouse_eu_spans"] > 0,
        "both workers took part": all(h["processed"] > 0 for h in results["worker_health"]),
        "calendar root cause found": results["calendar_root_cause"][0] == "Bad LLM output",
        "web pages": "FAIL" not in browse,
    }.items() if not ok]
    print("ALL CHECKS PASSED" if not bad else f"FAILED: {bad}", flush=True)
    if bad:
        sys.exit(1)
finally:
    cleanup()
