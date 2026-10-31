# Tracedown

**When an AI agent fails, Tracedown finds the step that caused it, not just the step that crashed. Then you replay the run with your fix.**

![Tracedown finds the step that caused a failure, then replays the run with the fix](docs/demo.gif)

Agents fail quietly. A model writes "Friday" where a date belongs. A retriever returns last year's policy. A loop burns 40 tool calls. A run looks fine but books the wrong flight. The error, if there is one, shows up several steps later. Tracedown records every step and walks back from the symptom to the cause:

- **AI stack traces.** Every failed run gets a pasteable report: where it surfaced, what caused it, the evidence, and the step to replay from.
- **Replay with a fix.** Earlier steps come from the recording, and from the step you choose onward the agent runs live with your change, so you can check a fix without re-running the whole conversation.
- **"Who is at fault?"** A verdict on every failed run: the agent, the user, or nobody yet, because many "agent failures" turn out to be the user's.
- **Works with what you have.** One line for LangGraph, the OpenAI Agents SDK, CrewAI, AutoGen, Semantic Kernel and LlamaIndex. You can also import runs you already record in **Langfuse**, **Arize Phoenix** or any **OpenTelemetry** exporter.

Self-hosted, Apache-2.0, and every feature is free, including the enterprise ones: single sign-on, roles, audit log, PII policies and scale-out storage. There is no paid edition. The SDK has no dependencies. An LLM is optional: the rules work without one, and an inexpensive judge model makes them sharper.

## Try it in a minute

```bash
git clone https://github.com/nbn07/agent-tracedown && cd agent-tracedown
pip install ./sdk/python ./server

python examples/run_demo.py          # five agents fail in realistic ways; no API keys, no server
```

**Already using Langfuse, Phoenix or OpenTelemetry?** Diagnose the runs you have, with no code changes:

```bash
export LANGFUSE_PUBLIC_KEY=pk-lf-... LANGFUSE_SECRET_KEY=sk-lf-...   # LANGFUSE_HOST for self-hosted
tracedown-import langfuse --errors-since 24h       # every run with an error in the last day
tracedown-import otlp traces.json --failed         # an OTel export (GenAI conventions, OpenInference, OpenLLMetry)
```

**Want the web app?** It shows runs, an interactive step graph of each run, the agent network, incidents, drift, analytics and a live feed.

```bash
tracedown-server --db demo.db                                 # API on http://localhost:7070
python examples/run_demo.py --endpoint http://localhost:7070
cd web && npm install && npm run dev                          # http://localhost:3000
```

Or start everything at once: `cd deploy && docker compose up -d`. The web app is at :3000 and the API at :7070.

![Run detail: the AI stack trace, who is at fault, and the step graph with the cause highlighted](web/docs-screenshots/run-detail.png)

## How well does it work?

We measured it on public benchmarks and published every number, including the ones where Tracedown loses.

**[Who&When](realworld/whowhen/README.md)** (ICML 2025) is the standard benchmark for "which agent, at which step, caused this failure?". It has 184 failed multi-agent runs, labelled by people. Scoring is exact match, run once, with no tuning on this data:

| Found the exact step that caused the failure | Algorithm-generated logs | Hand-crafted logs |
|---|---|---|
| **Tracedown + LLM judge** (Claude Haiku 5.5) | **36.5%** | **20.7%** |
| Tracedown, rules only (no model) | 21.4% | 19.0% |
| Simple rule: first failure-looking output | 23.0% | 15.5% |
| The paper's GPT-4o methods (best in each column) | 16.6% | 8.8% |

**[τ-bench](realworld/README.md)**: 499 real customer-service failures. Here a simple rule, "blame the first database change", still beats Tracedown's first guess (74% vs 68%). Tracedown's top three suspects contain the right step 81% of the time. A spot check by a second AI found the benchmark's own labels questionable in about half of a 50-run sample, so we treat these numbers as approximate.

The method, the labelling rules and every change made after the first scoring are in [realworld/LABELING.md](realworld/LABELING.md). The judge's answers are saved in the repo, so anyone can re-check the scores without an API key.

## What a report looks like

```
AgentFailureException: Invalid tool arguments
  run      72350035…  CalendarAssistant  2026-10-31 02:22 UTC
  at        step 5  PlannerAgent → execute_tool calendar_create_event
              start must be ISO-8601 date-time, got 'Friday'
  caused by step 4  PlannerAgent → chat gpt-4o-mini
              PlannerAgent produced 'Friday' at step 4, which calendar_create_event() could not use
  root cause: Bad LLM output (confidence 0.88)
  evidence:   step 4 output 'Friday' is used verbatim in step 5 arguments
  evidence:   step 5 error names the same value
  next:       replay from step 4 with your fix: tdn.replay(bundle, mode="from-step", from_step=4)
```

When the user, not the agent, caused the failure, the report says so:

```
  fault:      the user, not the agent (0.80): the user's details matched no account: 3 lookups failed and none succeeded
```

The rules behind the verdict are cautious: they were right both times they blamed the user on a small check, but they catch only a minority of user-caused failures. With an LLM judge (`TDN_JUDGE`), Tracedown finds about half of them, but it is wrong 4 times in 10 when it blames the user. Treat that verdict as a lead to check, not a ruling.

## Instrument your agent

```python
import agent_tracedown as tdn
tdn.init(project="support-bot", endpoint="http://localhost:7070", api_key="tdn_...", stream=True)

with tdn.run("CalendarAssistant", input=user_msg, prompt_version="extract@v3") as run:
    run.tools(TOOL_DEFINITIONS)                         # enables the wrong-tool check
    with tdn.agent("PlannerAgent"):
        chunks = tdn.retrieval(query, retriever.search, kb_snapshot="kb-2026-10-28")
        out = tdn.llm_call(call_model, model="gpt-4o-mini", prompt=messages)
        result = tdn.tool_call("calendar_create_event", args, create_event, schema=SCHEMA)
    run.output(answer)
```

### Frameworks

| Framework | One line |
|---|---|
| LangChain / LangGraph | `graph.invoke(x, config={"callbacks": [tdn_lc.callback("MyGraph")]})` |
| OpenAI Agents SDK | `tdn_oa.instrument()` |
| CrewAI | `tdn_crew.instrument()` |
| AutoGen AgentChat | `async for m in tdn_ag.trace_stream(team.run_stream(task=t), name="Team")` |
| Semantic Kernel | `tdn_sk.instrument(kernel)` |
| LlamaIndex | `tdn_li.instrument()` |
| OpenAI / Anthropic clients | `tdn.plugins.instrument("openai", client)` |

Import them from `agent_tracedown.integrations` (`langchain`, `openai_agents`, `crewai`, `autogen`, `semantic_kernel`, `llamaindex`). Each adapter is tested against the real library: LangGraph 1.2, openai-agents 0.24, CrewAI 1.15, AutoGen 0.7, Semantic Kernel 1.46 and LlamaIndex 0.14. The adapters observe the framework. Replay substitutes outputs only for calls that go through `tdn.llm_call` / `tdn.tool_call` or an instrumented model client.

### Import from Langfuse and OpenTelemetry

```bash
tracedown-import langfuse --trace-id 7f3a9c... --failed     # a run a user complained about
tracedown-import langfuse-file trace.json                    # a trace exported from the Langfuse UI or API
tracedown-import otlp traces.json --judge anthropic:claude-haiku-5-5
```

Everything runs locally by default. The options:
- `--send http://localhost:7070` also stores the runs in a Tracedown server.
- `--save runs.jsonl` keeps the converted runs.
- `--judge` adds the LLM judge.

A Tracedown server also accepts OTLP/JSON at `POST /v1/traces`. Sample exports to try are in [`examples/imports/`](examples/imports/).

### Send to your existing observability stack

```python
tdn.init(exporter=tdn.FanoutExporter(tdn.HttpExporter("http://tdn:7070"),
                                     tdn.OtlpHttpExporter("http://otel-collector:4318/v1/traces")))
```

The server can do the same for every run (`TDN_OTLP_FORWARD=...`). Prometheus scrapes `/metrics`.

## Replay a failure with your fix

```python
bundle = tdn.fetch_bundle("http://localhost:7070", trace_id)
with tdn.replay(bundle, mode="from-step", from_step=4):
    calendar_agent(extractor=fixed_extractor)     # earlier steps replay from the recording
```

Open `#/diff/<original>/<replay>` in the UI for the step-by-step comparison. The replay modes:
- `exact`: everything replays from the recording;
- `from-step`: the recording up to a step, then the agent runs live;
- `swap`: a new model or prompt;
- `full`: everything runs live.

## Everything in the box

| Capability | What you get |
|---|---|
| **Execution tracing** | Every step as OpenTelemetry-compatible spans: agent, LLM, tool, retrieval, memory, decision, handoff. Payloads are stored once, by content hash |
| **AI stack traces** | A pasteable report for every failed run |
| **Root-cause engine** | 9 detectors and a data-flow walk back to the earliest step whose output caused the failure. Optional LLM judge for the semantic cases |
| **Who is at fault** | A verdict for each failed run: agent, user, or unfinished |
| **Replay** | Exact, from a step, with a swapped model or prompt, or fully live. Recorded tool, retrieval and memory outputs are fed back, with a side-by-side diff |
| **Hallucination investigator** | Each claim in an answer is linked to its source and labelled supported, partial, stale, unsupported or contradicted |
| **Drift detection** | Compares runs across prompt versions, models, tool schemas or knowledge-base snapshots, and writes a report automatically |
| **Web app** | Next.js: runs, an interactive step graph of each run with the cause highlighted, payloads, claims, replay, run comparison; light and dark themes |
| **Multi-agent views** | Agent networks with handoffs and error rates, and a live span stream |
| **Incidents** | Runs grouped by root cause, with severity, timeline and suggested fix. Notifications to Slack, PagerDuty, GitHub Issues or a webhook |
| **Analytics** | Success rate, latency, tokens and cost per task, tool reliability, retrieval effectiveness, user feedback |
| **OpenTelemetry for AI** | `gen_ai.*` conventions in and out. Importers for Langfuse, OpenInference and OpenLLMetry |
| **Enterprise** | RBAC, OIDC single sign-on, hash-chained audit log, PII policies, Kafka/Redpanda ingest, multi-cluster ClickHouse |

## Deploy

| Layout | Metadata | Spans | Ingest | For |
|---|---|---|---|---|
| One process | SQLite | SQLite | in-process | a laptop, a demo, one team |
| Standard (Compose or Helm) | Postgres | Postgres | in-process, Redis cache | most teams; several API replicas |
| Scale (Compose `--profile scale` or Helm) | Postgres | ClickHouse, one or more clusters routed by project | Kafka + workers | high span volume, data residency |

```bash
helm install tracedown deploy/helm/tracedown                       # server, worker, web, Postgres, Redis
helm install tracedown deploy/helm/tracedown \
  --set redpanda.enabled=true,clickhouse.enabled=true,ingress.enabled=true   # the scale layout
```

You can bring your own Postgres, Redis, Kafka and ClickHouse instead. The chart also covers autoscaling, disruption budgets, network policies and non-root containers. Details are in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

> **Status: v0.3, beta.** About 100 unit tests run on both SQLite and Postgres, including the framework adapters against the real libraries. Integration tests run against Kafka, two ClickHouse clusters and Postgres. A full-stack test ([`deploy/e2e/stack.py`](deploy/e2e/stack.py)) runs the whole system (Postgres, Redis, a Kafka-protocol broker, two ClickHouse clusters, two API processes, two workers and the web app), sends real agent runs through it and checks every page in a browser. Design: [docs/DESIGN.md](docs/DESIGN.md).

## License

Everything in this repository is Apache-2.0: the SDK, the server, the root-cause engine and judge, the importers, the web viewer and the enterprise features. There is no paid tier, no feature held back, and no license key. Use it at work, change it and ship it. Contributions are welcome under the same license (see [CONTRIBUTING.md](CONTRIBUTING.md)).

## Configuration

| Variable | Purpose |
|---|---|
| `TDN_DB` | SQLite path for metadata (single-process setups) |
| `TDN_DATABASE_URL` | Postgres for metadata, e.g. `postgresql://user:pass@host/tracedown` (`TDN_DATABASE_POOL` connections per process) |
| `TDN_REDIS_URL` | Redis: shared read cache, live feed across replicas, ingest rate limits (`TDN_CACHE_TTL`, `TDN_INGEST_RATE_LIMIT`) |
| `TDN_API_KEYS` | Bootstrap owner keys (comma-separated). Without keys, users or this, the server runs open (dev) |
| `TDN_AUTH=required` | Refuse open mode even on a fresh install |
| `TDN_JUDGE` | Optional LLM judge, e.g. `anthropic:claude-haiku-5-5` or `openai:gpt-4.1-mini` (uses `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`) |
| `TDN_OIDC_ISSUER`, `_CLIENT_ID`, `_CLIENT_SECRET`, `_REDIRECT_URI`, `_ROLE_MAP`, `_ALLOWED_DOMAINS` | Single sign-on |
| `TDN_SESSION_SECRET` | Signs SSO session cookies (set it in production) |
| `TDN_CLICKHOUSE` | ClickHouse URL, or JSON router config for multi-cluster |
| `TDN_KAFKA_BOOTSTRAP` | Ingest through Kafka/Redpanda; run `tracedown-worker`. The topic is created if missing (`TDN_KAFKA_PARTITIONS`, default 12) |
| `TDN_OTLP_FORWARD`, `TDN_OTLP_HEADERS` | Forward finished runs to an OTLP endpoint |
| `TDN_PRICES` | Model prices, USD per million tokens: `{"model": {"input": .., "output": ..}}` |
| `TDN_DRIFT_MIN_RUNS` | Runs per cohort before a drift report (default 20) |
| `TDN_PUBLIC_URL` | Used in notification links and the cookie `secure` flag |


## Repository

```
spec/                 TDN telemetry spec (JSON Schema)
sdk/python/           SDK: tracer, exporters, plugins, framework integrations, redaction
server/tracedown_server/    API, pipeline, rca/, claims/, drift/, incidents/, importers/, storage/ (SQLite, Postgres), cache (Redis), enterprise/
web/                  Next.js web app (React, Tailwind, React Flow, Recharts)
benchmark/            TDN-RCA-Bench: synthetic generator, data, scorer
realworld/            evaluation on real failures: Who&When, τ-bench, live harnesses, label checks
docs/                 design, standards proposal (OTel GenAI SIG)
deploy/               Dockerfiles, Compose (standard / scale), Helm chart, SQL DDL, full-stack test (e2e/)
examples/             demo agents, the end-to-end demo, sample OTel/Langfuse exports (imports/)
```

## Standards

[docs/standards/otel-genai-proposal.md](docs/standards/otel-genai-proposal.md) proposes three things to the OpenTelemetry GenAI SIG, building on the in-flight memory and durable-execution work:
- decision spans;
- replay provenance with content digests;
- two memory consistency fields.

## Development

```bash
pip install -e ./sdk/python -e "./server[dev]"
pytest                                     # core
python benchmark/score.py --split holdout  # synthetic RCA benchmark
python realworld/whowhen/score.py          # Who&When (after realworld/whowhen/convert.py)
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Apache-2.0.
