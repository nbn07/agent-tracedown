# Diagnose runs you already have

No code changes to your agent. Tracedown reads exports from Langfuse, Arize Phoenix and any OpenTelemetry exporter.

## Paste a trace

In the web app, open **Diagnose a trace**, then paste or drop a file:

- a Langfuse trace export (a trace with its observations, or a list of observations),
- an OTLP/JSON export (one object, a list, or one object per line), for example from the OpenTelemetry Collector's file exporter or Arize Phoenix,
- a Tracedown bundle (`GET /api/v1/runs/<id>/bundle`).

The run is stored in the project `pasted` and opens with its diagnosis. The same works from a script:

```bash
curl -X POST --data-binary @trace.json http://127.0.0.1:7070/api/v1/import
```

The public demo is read-only and does not accept traces. Run `tracedown up` on your own machine for this.

## Import many runs from the command line

```bash
export LANGFUSE_PUBLIC_KEY=pk-lf-... LANGFUSE_SECRET_KEY=sk-lf-...
tracedown-import langfuse --errors-since 24h            # every trace with an error in the last day
tracedown-import langfuse --trace-id <id>               # one trace
tracedown-import langfuse-file export.json              # a downloaded export
tracedown-import otlp traces.json --failed              # an OTLP/JSON file; --failed marks the runs failed
```

Each run's diagnosis is printed in the terminal. Add `--send http://127.0.0.1:7070` to also store the runs in a Tracedown server, and `--judge anthropic:<model>` or `--judge openai:<model>` for an LLM second opinion on weak findings.

A step-by-step walkthrough with a sample file: [Diagnose your Langfuse failures in 5 minutes](tutorials/langfuse.md).

## Send OpenTelemetry directly

Tracedown is also an OTLP/HTTP JSON receiver. Point any exporter that emits OpenTelemetry GenAI spans at:

```
POST http://127.0.0.1:7070/v1/traces?project=<name>
```

## What imported runs can and cannot do

Imported runs get the full diagnosis: root cause, evidence, who was at fault and claim checking. Some checks need details that only the SDKs record, such as the list of tools offered to the model, so fewer of them fire. Replay needs a run recorded with the [Python or TypeScript SDK](integrations.md), because it runs your agent's own code.
