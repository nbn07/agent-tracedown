# Design Decisions

Why Tracedown is built the way it is. Each entry: the decision, the reason, and what it costs. To propose a new one, open an issue or a [Discussion](https://github.com/nbn07/agent-tracedown/discussions) before writing code.

### Rules first, LLM optional
**Decision:** the diagnosis starts with deterministic detectors. An LLM judge is only asked when the rules are unsure, and only if you turn it on.
**Why:** rules are free, fast, private (nothing leaves your machine) and give the same answer every time, so a verdict can be checked and a regression can be tested.
**Cost:** rules miss judgement-call mistakes ("refunded the wrong order"). That is what the judge is for, and why the rules-only numbers are lower.

### Every finding carries evidence a person can check
**Decision:** no detector may report a finding without plain-language evidence ("step 4 output 'Friday' is used verbatim in step 5 arguments").
**Why:** a wrong verdict with evidence is quickly spotted; a wrong verdict without it wastes an afternoon.

### Scores rank; they are not shown as percentages
**Decision:** the app and stack trace say "top suspect" and list suspects in order. Scores stay in the API as `confidence` for ranking.
**Why:** we measured it, and a high score does not mean a more likely right answer ([Accuracy and Benchmarks](Accuracy-and-Benchmarks)). Showing "88%" would promise something the number can't deliver.

### Blame the cause, not the symptom
**Decision:** walk back from where the failure showed up, through the data that flowed between steps, to the earliest suspicious step.
**Why:** the step that crashed is often doing its job correctly (the calendar tool *should* reject "Friday").

### A run that changed something in the world: suspect the action
**Decision:** when a failed run refunded, booked or sent something, wording problems are never ranked above the best finding on that action.
**Why:** on customer-service benchmarks, the decisive mistake is usually the action, not the words around it.

### The Python SDK uses only the standard library and never raises
**Decision:** no dependencies; telemetry failures are recorded, not raised.
**Why:** a debugging tool must never be the reason an agent breaks, and installing it should never clash with your other packages.

### OpenTelemetry names first
**Decision:** use `gen_ai.*` attributes from the OpenTelemetry GenAI conventions; add a `tdn.*` attribute only when none exists, and record it in `spec/tdn-event.schema.json`.
**Why:** your traces stay readable by other tools, and imports from OpenTelemetry work without translation.

### Work on top of the traces you already have
**Decision:** import from Langfuse and OpenTelemetry, and paste a trace in the web app, rather than asking you to re-instrument.
**Why:** most teams already trace. Tracedown should add the "why", not replace the "what".

### One command to try it
**Decision:** `tracedown up` starts the server, the bundled web app and eight demo failures; the default database lives in `~/.tracedown/`.
**Why:** people decide in the first five minutes. The home folder avoids cloud-synced folders (OneDrive, iCloud), which lock database files.

### Replays never cause real side effects by default (v0.5)
**Decision:** while checking a fix, tools that would run for real are blocked unless you give a stand-in or name them as safe.
**Why:** a check must never refund, book or email anyone.

### Apache-2.0, every feature free
**Decision:** no paid edition, no features held back (SSO, Postgres, ClickHouse, Helm are all in the open repo).
**Why:** trust and adoption. Debugging tools are used where data is sensitive, and people need to read and run all of it.
