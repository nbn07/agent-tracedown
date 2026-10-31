# Diagnose your Langfuse failures in 5 minutes

A step-by-step walkthrough to import existing Langfuse traces into Tracedown and get an AI stack trace that points to the root cause.

No account is required for the sample. Real Langfuse credentials are only needed for the live fetch step.

## 1. Install

From a clone of this repository:

```bash
git clone https://github.com/nbn07/agent-tracedown && cd agent-tracedown
pip install ./sdk/python ./server
```

Once the package is on PyPI you can use `pip install agent-tracedown-server` instead.

## 2. Try the sample export (no account)

Sample Langfuse traces live in `examples/imports/`:

```bash
tracedown-import langfuse-file examples/imports/langfuse-trace.json
```

You should see an AI stack trace similar to:

```
━━ ResearchAgent · trace lf-trace-42 · 4 steps · failure
AgentFailureException: Unsupported claim (hallucination)
  run      lf-trace…  ResearchAgent  2026-10-29 14:00 UTC
  at        step 3  ResearchAgent → answer
              1 of 1 answer claims not supported by any source
  root cause: Unsupported claim (hallucination) (confidence 0.75)
  evidence:   unsupported: "Acme Corp's 2025 revenue was $4.2 billion, up 12% year over year." (number(s) 12, 4.2 appear in no source)
  also:       Tool failure (step 2, 0.30)
  next:       replay from step 3 with your fix: tdn.replay(bundle, mode="from-step", from_step=3)
```

### What each line means

- **Header** — agent name, truncated trace id, step count, and outcome (failure / success).
- **Exception type** — the high-level diagnosis (here an unsupported claim / hallucination).
- **at step N** — the step where the symptom was observed.
- **caused by / root cause** — the earlier step that produced the bad value, with a confidence score.
- **evidence** — concrete facts a human can check (quoted text, missing numbers, error messages).
- **also** — secondary findings.
- **next** — suggested next action, usually a replay from the broken step.

## 3. Connect a real Langfuse project

Export the public and secret keys from your Langfuse project settings (cloud or self-hosted):

```bash
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...
# optional for self-hosted
export LANGFUSE_HOST=https://your-langfuse.example.com
```

Fetch every trace that contains an ERROR observation in the last 24 hours:

```bash
tracedown-import langfuse --errors-since 24h
```

Useful flags (see `tracedown-import langfuse --help` for the full list):

- `--limit 50` — cap the number of traces
- `--trace-id <id>` — import one specific trace
- `--failed` / `--succeeded` — force the outcome label
- `--save runs.jsonl` — also write the runs as JSON lines
- `--quiet` — suppress the printed analysis

## 4. Optional: send to a Tracedown server

Start a local server (no Docker required):

```bash
tracedown-server --db demo.db
```

Then re-run the import with `--send`:

```bash
tracedown-import langfuse-file examples/imports/langfuse-trace.json \
  --send http://localhost:7070
```

Open the web app (in another terminal):

```bash
cd web && npm install && npm run dev
```

Visit http://localhost:3000 to browse the imported runs, stack traces, and evidence.

## 5. Optional: LLM judge

Rules-only diagnosis is free and private. For sharper results on ambiguous cases add an inexpensive judge:

```bash
tracedown-import langfuse-file examples/imports/langfuse-trace.json \
  --judge anthropic:claude-haiku-5-5
```

(Requires the corresponding provider API key in the environment.)

## Further reading

- CLI source and every flag: `server/tracedown_server/importers/cli.py`
- README section “B. Diagnose the runs you already have”
- Other sample exports in `examples/imports/`
