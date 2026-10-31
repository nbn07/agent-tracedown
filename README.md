# Tracedown

**The flight recorder for AI agents.** When an agent picks the wrong tool, loops, quotes an outdated document or invents a number, Tracedown tells you *which step caused it*, not just which step crashed. Then it lets you replay the run with a fix.

```
AgentFailureException: Invalid tool arguments
  run      1babef60…  CalendarAssistant  2026-10-30 03:24 UTC
  at        step 5  PlannerAgent → execute_tool calendar_create_event
              start must be ISO-8601 date-time, got 'Friday'
  caused by step 4  PlannerAgent → chat gpt-4o-mini
              PlannerAgent produced 'Friday' at step 4, which calendar_create_event() could not use
  root cause: Bad LLM output (confidence 0.88)
  evidence:   step 4 output 'Friday' is used verbatim in step 5 arguments
  evidence:   step 5 error names the same value
  next:       tracedown replay 1babef60 --from-step 4
```

> **Status: v0.2.** Everything below runs and is tested: 66 server and SDK tests, plus 6 framework tests against the real libraries. Not done yet: the Postgres metadata store (metadata is SQLite today), the Next.js/React Flow UI (a single-page viewer ships instead), and live testing against a real Kafka broker and a ClickHouse server (both are tested through test doubles and embedded ClickHouse). Design: [docs/DESIGN.md](docs/DESIGN.md).

## Features

| # | Capability | What you get |
|---|---|---|
| 1 | **Execution tracing** | Every step (agent, LLM, tool, retrieval, memory, decision, handoff) as OpenTelemetry-compatible spans; payloads stored once by SHA-256 |
| 2 | **AI stack traces** | A pasteable "exception" for every failed run: where it surfaced, what caused it, evidence, next step |
| 3 | **Root-cause engine** | 9 detectors + a data-flow walk back to the earliest step whose output caused the failure; ranked candidates with evidence |
| 4 | **Replay** | `exact`, `from-step`, `swap` (new model or prompt) and `full`; recorded tool, retrieval and memory outputs fed back; side-by-side diff |
| 5 | **Hallucination investigator** | Answer split into claims; each linked to its supporting chunk or tool result, labelled supported / partial / stale / unsupported / contradicted, traced to the LLM step that wrote it, painted as a heatmap |
| 6 | **Drift detection** | Cohorts by prompt version, model, tool schema, knowledge-base snapshot or release; z-test on success rate, Jensen-Shannon divergence on tool mix; reports written automatically when a new version has enough runs |
| 7 | **Multi-agent visualization** | Per-run and cross-run agent networks with handoffs and error rates; live span stream (Server-Sent Events) |
| 8 | **Incidents** | Runs clustered by root-cause signature; severity, timeline, suggested fix, regression on recurrence; Slack, PagerDuty, GitHub Issues and webhook hooks |
| 9 | **Performance analytics** | Success rate, latency p50/p95, tokens and cost per task, hallucination rate, response quality, tool reliability, retrieval effectiveness, user feedback |
| 10 | **OpenTelemetry for AI** | `gen_ai.*` conventions plus `tdn.*`; OTLP in (`/v1/traces`) and out (SDK exporter or server forwarding to Jaeger, Tempo, the Datadog Agent or any collector); Prometheus `/metrics` |
| ★ | **Enterprise** | RBAC (viewer / developer / admin / owner, project-scoped API keys), OIDC SSO with group-to-role mapping, hash-chained audit log, server-side PII policies, Kafka/Redpanda ingest, multi-cluster ClickHouse |

## Quick start

```bash
git clone https://github.com/nbn07/agent-tracedown && cd agent-tracedown
pip install ./sdk/python ./server

tracedown-server --db demo.db                                   # http://localhost:7070
python examples/run_demo.py --endpoint http://localhost:7070
```

or `docker compose -f deploy/docker-compose.yml up`.

The demo needs no API keys. It runs agents that fail in realistic ways:
- a wrong date format;
- an outdated policy document;
- a search loop;
- a made-up price;
- the wrong tool for a request.

It then repeats one failure to open an incident, ships a prompt change that triggers a drift report, and replays a failure with a fix. Open the UI to explore runs, incidents, drift, analytics and the live feed.

### Who is at fault?

Not every failed run is an agent bug. Every failed run gets a verdict: `agent`, `user` (the user's details matched no account, or they declined), or `unfinished` (the run stopped while the agent was waiting). When it isn't the agent, the stack trace says so:

```
  fault:      the user, not the agent (0.80): the user's details matched no account: 3 lookups failed and none succeeded
```

The rules are deliberately cautious: right in 2 of 2 cases on a small check, but they catch only a minority of user-caused failures. With `TDN_JUDGE` set, an LLM judge decides the harder cases, such as the user choosing the wrong item themselves. On a small check against another AI's labels, the judge found half of the user-caused failures but was wrong 4 times in 10 when it blamed the user, so treat its verdict as a lead to check, not a ruling.

## Already recording with Langfuse, Phoenix or OpenTelemetry?

Diagnose the runs you already have, with no code changes. Everything runs locally unless you add `--send`.

```bash
pip install ./sdk/python ./server     # from a clone of this repo (see Quick start)

# Langfuse (cloud or self-hosted): every trace with an error in the last day
export LANGFUSE_PUBLIC_KEY=pk-lf-... LANGFUSE_SECRET_KEY=sk-lf-...   # LANGFUSE_HOST for self-hosted
tracedown-import langfuse --errors-since 24h
tracedown-import langfuse --trace-id 7f3a9c... --failed     # a run a user complained about

# Any OpenTelemetry export: OTel GenAI conventions, Arize Phoenix / OpenInference, OpenLLMetry
tracedown-import otlp traces.json --failed

# A trace exported from the Langfuse UI or API
tracedown-import langfuse-file trace.json
```

```
━━ SupportAgent · trace 5b8efff7… · 6 steps · failure
  caused by step 4  SupportAgent → refund_order
              First state-changing call before the failure: refund_order()
  evidence:   arguments: {"order_id": "#1001"}
   suspects:
   1. step 4    Suspect action (first state change): refund_order()
   2. step 5    Unsupported claim (hallucination): 1 of 2 answer claims not supported by any source
```

Options:
- `--send http://localhost:7070` also stores runs in a Tracedown server, with the UI, incidents and drift.
- `--judge anthropic:claude-haiku-5-5` adds the LLM judge.
- `--save runs.jsonl` keeps the converted runs.

A Tracedown server also accepts OTLP/JSON directly at `POST /v1/traces`. Try it on the samples in [`examples/imports/`](examples/imports/).

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
    calendar_agent(extractor=fixed_extractor)     # steps before 4 come from the recording
```

Open `#/diff/<original>/<replay>` in the UI for the step-by-step comparison.

## How well does root-cause analysis work?

On the bundled benchmark, [TDN-RCA-Bench v0](benchmark/README.md): 10 failure families, two thirds of cases with distractors.

| | holdout split (never tuned on) | test split, first run |
|---|---|---|
| Tracedown, correct cause *and* step | 100% | 93% |
| "first error span" baseline | 10% | — |

These are synthetic runs from the same ten scenario structures the engine was built around. They show robustness to wording and noise, not to unseen failure shapes.

**On a public benchmark labelled by people:** [Who&When](realworld/whowhen/README.md) (ICML 2025) has 184 failed multi-agent runs, each with the agent and step that caused the failure. Scoring is exact match, with no ground truth given to the judge.

| | Right step: algorithm-generated logs | Right step: hand-crafted logs |
|---|---|---|
| **Tracedown + LLM judge (Claude Haiku 5.5)** | **36.5%** | **20.7%** |
| Tracedown, rules only (no model) | 21.4% | 19.0% |
| Simple rule: first failure-looking output | 23.0% | 15.5% |
| Paper's GPT-4o methods (best in each column) | 16.6% | 8.8% |

**On real failures** ([realworld/](realworld/README.md)): 499 failed τ-bench runs by Claude 3.5 Sonnet, held out while tuning on GPT-4o, with a labelling protocol fixed before scoring.

| | right step, top 1 | right step, top 3 | names the kind of mistake |
|---|---|---|---|
| Tracedown (rules only) | 68% | 81% | 1% |
| Tracedown + LLM judge (Claude Haiku 5.5) | 63% | 81% | 45% |
| "first state-changing call" baseline | 74% | 74% | 29% |

The rules find the right neighbourhood but lose to a simple prior on the first guess. Most real failures are the right action with the wrong item, which rules cannot see. The optional LLM judge (`TDN_JUDGE`) fixes the *why* (1% → 45% correct mistake kind) but not the *where*. A label check by a second AI (Microsoft Copilot) agreed with τ-bench's mechanical labels on only about half of a 50-run sample, so treat these τ-bench numbers as approximate ([details](realworld/LABELING.md)). `realworld/live/` also has harnesses that run the OpenAI Agents SDK customer-service example, a LangGraph SQL agent and τ-bench live with your own model.

## Configuration

| Variable | Purpose |
|---|---|
| `TDN_DB` | SQLite path for metadata |
| `TDN_API_KEYS` | Bootstrap owner keys (comma-separated). Without keys, users or this, the server runs open (dev) |
| `TDN_AUTH=required` | Refuse open mode even on a fresh install |
| `TDN_OIDC_ISSUER`, `_CLIENT_ID`, `_CLIENT_SECRET`, `_REDIRECT_URI`, `_ROLE_MAP`, `_ALLOWED_DOMAINS` | Single sign-on |
| `TDN_SESSION_SECRET` | Signs SSO session cookies (set it in production) |
| `TDN_CLICKHOUSE` | ClickHouse URL, or JSON router config for multi-cluster |
| `TDN_KAFKA_BOOTSTRAP` | Ingest through Kafka/Redpanda; run `tracedown-worker` |
| `TDN_OTLP_FORWARD`, `TDN_OTLP_HEADERS` | Forward finished runs to an OTLP endpoint |
| `TDN_PRICES` | Model prices, USD per million tokens: `{"model": {"input": .., "output": ..}}` |
| `TDN_DRIFT_MIN_RUNS` | Runs per cohort before a drift report (default 20) |
| `TDN_PUBLIC_URL` | Used in notification links and cookie `secure` flag |

## Repository

```
spec/                 TDN telemetry spec (JSON Schema)
sdk/python/           SDK: tracer, exporters, plugins, framework integrations, redaction
server/tracedown_server/    API, pipeline, rca/, claims/, drift/, incidents/, importers/, analytics, enterprise/, static UI
benchmark/            TDN-RCA-Bench: generator, data, scorer
docs/                 design, standards proposal (OTel GenAI SIG)
deploy/               Dockerfile, compose (dev / standard / scale), SQL DDL
examples/             demo agents, the end-to-end demo, sample OTel/Langfuse exports (imports/)
realworld/            evaluation on real failures: τ-bench, Who&When, live harnesses
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
python benchmark/score.py --split holdout  # RCA benchmark
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Apache-2.0.
