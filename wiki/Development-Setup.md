# Development Setup

## Option A: Codespaces (recommended, nothing to install)

1. On the [repository page](https://github.com/nbn07/agent-tracedown), click **Code → Codespaces → Create codespace** (or use [this link](https://codespaces.new/nbn07/agent-tracedown?quickstart=1)).
2. Wait about two minutes. `.devcontainer/setup.sh` installs the Python SDK and server in editable mode, the TypeScript SDK and web dependencies, and builds the web app.
3. In the terminal: `tracedown up --no-open`, then open the forwarded port **7070**.

Codespaces is free for personal accounts up to a monthly quota. Stop the codespace when you're done.

The same setup works locally in VS Code with **Dev Containers: Reopen in Container**.

## Option B: your own machine

Needs **Python 3.10+** and **Node 20+**.

```bash
pip install -e ./sdk/python -e "./server[dev]"
pytest                                       # SDK and server tests
node web/scripts/build-static.mjs            # build the web app into the server package (once, and after web changes)
tracedown up --db dev.db                     # server + web app + demo runs on http://127.0.0.1:7070
```

Working on the web app with live reload:

```bash
tracedown up --db dev.db --no-open           # terminal 1: the API on :7070
cd web && npm install && npm run dev         # terminal 2: the web app on :3000, talking to :7070
```

TypeScript SDK:

```bash
cd sdk/typescript && npm install && npm test
```

## What each test suite covers

| Command | Covers | CI job |
|---|---|---|
| `pytest` (repo root) | Python SDK and server, demo stories, import, read-only mode | `core` (Python 3.10, 3.12, 3.13) |
| `PYTHONPATH=sdk/python pytest sdk/python/tests/integrations` | Framework adapters against the real libraries (install the matching extra) | `frameworks (…)` |
| `npm test` in `sdk/typescript` | TypeScript SDK, including a check that it matches the Python demo diagnoses | `typescript (20/22/24)` |
| `npm run typecheck && npm run build` in `web` | Web app compiles | `web` |

## Handy commands

| Command | What it does |
|---|---|
| `tracedown up` | Server, web app and demo runs; opens the browser |
| `tracedown up --no-demo` | Start empty, for your own runs |
| `tracedown up --read-only` | Refuse all writes after loading the demo (like the public demo) |
| `tracedown demo` | Print the demo diagnoses in the terminal, no server |
| `tracedown mcp` | MCP server (stdio) for Claude Code, Cursor and other MCP clients; needs the `[mcp]` extra |
| `tracedown-import` | Import Langfuse, Phoenix or OTLP exports |
| `tracedown-server` | Production server (Postgres, Redis, several workers) |

Problems? See [Troubleshooting](Troubleshooting).
