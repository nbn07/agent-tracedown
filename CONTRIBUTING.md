# Contributing to Tracedown

Thanks for helping. The fastest ways to make a difference right now:

1. **A failing run we diagnose wrongly.** Open an issue with the run bundle (`GET /api/v1/runs/{id}/bundle`, redacted) and what the real root cause was. These become the RCA benchmark.
2. **A detector.** Add a function decorated with `@detector` in `server/tracedown_server/rca/detectors.py`; it receives a `RunView` and returns `Finding`s. Include a demo agent in `examples/` that triggers it and a test.
3. **A framework adapter.** See `sdk/python/agent_tracedown/integrations/`. Use `ManualTrace` when the framework reports events on other threads, add a test under `sdk/python/tests/integrations/` that drives the real library without network, and add an extra to `sdk/python/pyproject.toml`.
4. **A benchmark case.** See `benchmark/README.md`; real, redacted failures are the most valuable contribution of all.
5. **A plugin.** Subclass `Plugin` in `sdk/python/agent_tracedown/plugins/`, route calls through `tdn.llm_call` / `tdn.tool_call` so replay works, and `register()` it. Aim for about 100 lines and no hard dependency on the wrapped library.

## Ground rules

- The SDK stays stdlib-only and must never raise from telemetry code paths.
- Changing a span attribute means changing `spec/tdn-event.schema.json` in the same PR.
- Prefer `gen_ai.*` (OpenTelemetry GenAI conventions) over a new `tdn.*` attribute when one exists.
- Every detector reports evidence a human can check; no unexplained scores.

## Setup

```bash
pip install -e ./sdk/python -e "./server[dev]"
pytest                       # SDK + server tests
tracedown-server --db dev.db       # then: python examples/run_demo.py --endpoint http://localhost:7070
```

Larger changes: open an issue first, or add an ADR in `docs/adr/`.
