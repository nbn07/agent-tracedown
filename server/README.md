# agent-tracedown-server

The analysis side of [Tracedown](https://github.com/nbn07/agent-tracedown), the flight recorder for AI agents:

- **Root-cause analysis.** AI stack traces that name the step that caused a failure, with an optional LLM judge.
- **A "who is at fault" verdict** for every failed run: the agent, the user, or nobody yet.
- **`tracedown-import`.** Diagnose runs you already record in Langfuse, Arize Phoenix or any OpenTelemetry exporter, with no code changes.
- **`tracedown up`.** The server and the full web app in one command, with demo runs to explore. No Node or Docker needed.
- **`tracedown-server`.** Ingest API, web app, incidents, drift reports, analytics and replay diffs, for production (Postgres, Redis, several workers).

```bash
pip install agent-tracedown-server
tracedown up                                      # http://127.0.0.1:7070, opens your browser
tracedown-import langfuse --errors-since 24h      # uses LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY
tracedown-import otlp traces.json --failed
tracedown-server --db tracedown.db                # http://localhost:7070
```

Benchmarks, including the cases where Tracedown loses to simple baselines, are in the [repository](https://github.com/nbn07/agent-tracedown#how-well-does-it-work). Licensed under Apache-2.0.
