# Engineering design

The full engineering design (architecture diagram, event and database schemas, API,
root-cause engine, replay, plugin SDK, 12-week v1 plan and roadmap) is maintained as a
shared document:

**https://claude.ai/artifact/VNeHcxjz9cDuxJiLfbJeqC**

Summary of decisions that shape this repository:

- **Extend OpenTelemetry, never fork it.** Spans use `gen_ai.*` attributes from the OTel GenAI
  semantic conventions and add an `tdn.*` namespace only for what debugging and replay need
  (`spec/tdn-event.schema.json`).
- **Content-addressed payloads.** Prompts, completions, tool I/O and chunks are blobs keyed by
  SHA-256; spans carry digests. This deduplicates and makes replay a lookup.
- **One storage interface, three profiles.** `dev` = SQLite (this MVP), `standard` = Postgres +
  ClickHouse + MinIO, `scale` = the same behind Redpanda (`deploy/sql/`).
- **Diagnosis over dashboards.** The RCA engine walks data flow back from the symptom to the
  earliest flagged step. Heuristics run on every failed run; an LLM judge (v1.1) only on runs
  with no confident finding.
- **Replay executes in the SDK**, because only the SDK has the agent's code. The server plans
  which steps are recorded and which run live.

## Module map (v0.2)

| Area | Code |
|---|---|
| Ingest pipeline (analysis, profiles, incidents, drift, live events, PII, OTLP forward) | `server/tracedown_server/pipeline.py` |
| Root-cause engine and stack traces | `server/tracedown_server/rca/` |
| Hallucination investigator | `server/tracedown_server/claims/` |
| Drift | `server/tracedown_server/drift/` |
| Incidents and hooks | `server/tracedown_server/incidents/` |
| Analytics and Prometheus | `server/tracedown_server/analytics.py` |
| RBAC, SSO, audit, PII, queue, ClickHouse | `server/tracedown_server/enterprise/` |
| Framework adapters | `sdk/python/agent_tracedown/integrations/` |
| Benchmark | `benchmark/` |
| OTel proposal | `docs/standards/otel-genai-proposal.md` |

## v1 gates

| Phase | Weeks | Gate |
| --- | --- | --- |
| Capture | 1–3 | first trace in under 5 minutes |
| Diagnose | 4–6 | correct top-1 root cause on 80% of a labelled 50-run failure set (proposed target) |
| Replay + UI | 7–9 | exact replay reproduces every step |
| Harden | 10–12 | v1.0 tagged, load test published |
