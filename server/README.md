# agent-tracedown-server

The analysis side of [Tracedown](https://github.com/nbn07/agent-tracedown), the flight recorder for AI agents:

- **Root-cause analysis.** AI stack traces that name the step that caused a failure, with an optional LLM judge.
- **A "who is at fault" verdict** for every failed run: the agent, the user, or nobody yet.
- **`tracedown-import`.** Diagnose runs you already record in Langfuse, Arize Phoenix or any OpenTelemetry exporter, with no code changes.
- **`tracedown-server`.** Ingest API, web viewer, incidents, drift reports, analytics and replay diffs.

```bash
pip install agent-tracedown-server
tracedown-import langfuse --errors-since 24h      # uses LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY
tracedown-import otlp traces.json --failed
tracedown-server --db tracedown.db                # http://localhost:7070
```

Benchmarks, including the cases where Tracedown loses to simple baselines, are in the [repository](https://github.com/nbn07/agent-tracedown#how-well-does-it-work). Licensed under Apache-2.0.
