# Troubleshooting

### `pip` says "No matching distribution found for agent-tracedown-server"
Your Python is older than 3.10. Check with `python3 --version`. On a Mac, `brew install python@3.12`, then `python3.12 -m venv .venv && source .venv/bin/activate`. Or skip installing: `uvx --from agent-tracedown-server tracedown up`.

### "attempt to write a readonly database"
The database file is in a cloud-synced folder (OneDrive, iCloud, Dropbox, Google Drive), and the sync client locked it. Since v0.4.1 the default database is `~/.tracedown/tracedown.db`, outside those folders, and `tracedown up` warns you. If you pass `--db`, use a local path.

### "Port 7070 is in use"
Without `--port`, `tracedown up` picks the first free port from 7070 to 7079 and prints the address. With `--port`, pick another one: `tracedown up --port 7080`.

### I see a plain page instead of the full web app
That install has no bundled web app (for example, a source checkout). Build it once: `node web/scripts/build-static.mjs`, or run the dev web app: `cd web && npm install && npm run dev`.

### The public demo says "This is a read-only demo"
It is: the [public demo](https://tracedown-demo.onrender.com/runs/) refuses every write so it stays the same for everyone. Diagnosing pasted traces still works. To try everything, run `tracedown up` on your machine.

### The public demo takes a minute to load
The free host sleeps when idle. The first visit wakes it up.

### My runs don't show up
- The SDK needs an endpoint: `tdn.init(endpoint="http://127.0.0.1:7070")`. Without one it prints spans to the console.
- A run is sent when it **ends**. Check the run's `with tdn.run(...)` block finished.
- The SDK never raises from telemetry; look for `[agent-tracedown] export failed:` on stderr.

### An imported run can't be replayed
Expected. Replay runs inside your agent's code, so it needs a run recorded with the Python or TypeScript SDK. Imported runs get the full diagnosis.

### "ReplayDivergence: step N input differs from the recording"
Your agent's code changed *before* the replay point, so the recording no longer matches. Replay from an earlier step (`mode="from-step"`), or use `swap` mode to re-run model calls only.

### Tracedown blamed the wrong step
Check the next two suspects first: the right step is often in the top three. Then please [report it](Reporting-a-Wrong-Diagnosis).

### Still stuck?
Ask in [Discussions → Q&A](https://github.com/nbn07/agent-tracedown/discussions) or [open a bug](https://github.com/nbn07/agent-tracedown/issues/new?template=bug.yml). Include your OS, Python version and `tracedown up` output.
