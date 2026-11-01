# Contributing to Tracedown

Thanks for helping. New here? The [wiki](https://github.com/nbn07/agent-tracedown/wiki) walks you through [your first contribution](https://github.com/nbn07/agent-tracedown/wiki/Your-First-Contribution), [the architecture](https://github.com/nbn07/agent-tracedown/wiki/Architecture-Tour) and [writing a detector](https://github.com/nbn07/agent-tracedown/wiki/Writing-a-Detector).

Thanks for helping. The fastest ways to make a difference right now:

1. **A failing run we diagnose wrongly.** Open an issue with the run bundle (`GET /api/v1/runs/{id}/bundle`, redacted) and what the real root cause was. These become the RCA benchmark.
2. **A detector.** Add a function decorated with `@detector` in `server/tracedown_server/rca/detectors.py`; it receives a `RunView` and returns `Finding`s. Include a test, and a demo agent in `server/tracedown_server/demo/agents.py` if it makes a good story.
3. **A framework adapter.** See `sdk/python/agent_tracedown/integrations/` (Python) or `sdk/typescript/src/integrations/` (TypeScript). Use `ManualTrace` when the framework reports events on other threads, add a test under `sdk/python/tests/integrations/` that drives the real library without network, and add an extra to `sdk/python/pyproject.toml`.
4. **A benchmark case.** See `benchmark/README.md`; real, redacted failures are the most valuable contribution of all.
5. **A plugin.** Subclass `Plugin` in `sdk/python/agent_tracedown/plugins/`, route calls through `tdn.llm_call` / `tdn.tool_call` so replay works, and `register()` it. Aim for about 100 lines and no hard dependency on the wrapped library.

## Ground rules

- The SDK stays stdlib-only and must never raise from telemetry code paths.
- Changing a span attribute means changing `spec/tdn-event.schema.json` in the same PR.
- Prefer `gen_ai.*` (OpenTelemetry GenAI conventions) over a new `tdn.*` attribute when one exists.
- Every detector reports evidence a human can check; no unexplained scores.

## Setup

**In the browser, nothing to install:** on the repository page click **Code → Codespaces → Create codespace** (or use the [direct link](https://codespaces.new/nbn07/agent-tracedown?quickstart=1)). In about two minutes you have Python, Node, every dependency and the bundled web app, set up by `.devcontainer/setup.sh`. The same setup works locally with VS Code's "Dev Containers: Reopen in Container". Run `tracedown up --no-open` there and open the forwarded port 7070.

**On your own machine** (Python 3.10+, Node 20+):

```bash
pip install -e ./sdk/python -e "./server[dev]"
pytest                             # SDK + server tests, about 15 seconds
tracedown up --db dev.db           # server and demo runs on :7070; the full web app after
                                   #   `node web/scripts/build-static.mjs`
cd sdk/typescript && npm install && npm test    # TypeScript SDK
cd web && npm install && npm run dev            # web app on :3000, talking to the server on :7070
```

## Repository layout

```
spec/                     TDN telemetry spec (JSON Schema)
sdk/python/               Python SDK: tracer, exporters, plugins, framework integrations, redaction
sdk/typescript/           TypeScript SDK: same data and replay; Vercel AI SDK and OpenAI Agents integrations
server/tracedown_server/  API, pipeline, rca/, claims/, drift/, incidents/, importers/, pricing, storage/, enterprise/,
                          demo/ (the demo agents), cli.py (`tracedown up`), web/ (built web app, not committed)
web/                      Next.js web app (React, Tailwind, React Flow, Recharts)
benchmark/                TDN-RCA-Bench: synthetic generator, data, scorer
realworld/                evaluation on real failures: Who&When, τ-bench, live harnesses, label checks
wiki/                     the GitHub wiki's pages; edit here, a workflow publishes them
docs/                     design, deployment, configuration, standards proposal (OTel GenAI SIG)
deploy/                   Dockerfiles, Compose (standard / scale), Helm chart, full-stack test (e2e/), public demo (demo/)
.devcontainer/            the Codespaces / Dev Containers setup
examples/                 run_demo.py, sample OTel/Langfuse exports (imports/)
```

## Reporting a wrong diagnosis

Use the **Wrong diagnosis** issue form. Include what really went wrong and, if you can, the redacted run bundle. Each report becomes a benchmark case.

Larger changes: open an issue first, or add an ADR in `docs/adr/`.

## License

Tracedown is Apache-2.0, with every feature free and no paid edition. By submitting a contribution you agree it is licensed under Apache-2.0 too (section 5 of the license).
