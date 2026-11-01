# Glossary

| Term | Meaning | Everyday version |
|---|---|---|
| **Run** | One end-to-end attempt by an agent to handle a request. One run is one trace. | One trip in the car |
| **Span** | One recorded operation inside a run: a model call, a tool call, a retrieval, a memory read or write, a hand-off between agents. | One leg of the trip |
| **Step** | A span's position in the run (`tdn.step`), counted from 0. Verdicts name steps. | "At the third junction" |
| **Trace ID** | The ID of a run. | The trip's ticket number |
| **Blob** | The content of a span's input or output, stored separately and referred to by its digest (a hash). | The parcel; the span holds the receipt |
| **Bundle** | A run's spans plus its blobs: everything needed to diagnose or replay it (`GET /api/v1/runs/{id}/bundle`). | The full dashcam footage |
| **Detector** | A small rule that looks for one kind of problem and reports findings. | One test the mechanic runs |
| **Finding** | One thing a detector noticed: a category, a step, a score and evidence. | "Brake pads worn: 2 mm left" |
| **Symptom** | A finding where the failure *showed up* (a tool rejecting a date). | The warning light |
| **Suspect** | A finding that looks wrong in itself (a model writing "Friday" where a date belongs). | A worn part |
| **Root cause** | The top-ranked finding: the step Tracedown blames. | "Replace this part" |
| **Evidence** | Plain sentences explaining a finding, which you can check against the run. | The mechanic showing you the worn part |
| **Score** (`confidence` in the API) | A number used to put findings in order. Not a probability. | Order on the mechanic's list, not odds |
| **Point of no return** | In a failed run, the first call that changed something in the world (a refund, a booking). | The moment you drove off with the handbrake on |
| **Claim** | One sentence or clause of the agent's final answer, checked against its sources: supported, partial, unsupported, contradicted or stale. | Fact-checking a newspaper article line by line |
| **Judge** | An optional LLM asked for a second opinion when the rules are unsure (`TDN_JUDGE`). | Calling a specialist |
| **Fault** | Who was at fault: the agent, the user, or unfinished (nobody yet). | Driver error, bad directions from the passenger, or the trip isn't over |
| **Replay** | Re-running a recorded run, with some steps served from the recording and the rest live. Modes: `exact`, `from-step`, `swap`, `full`. | A test drive |
| **Fix check** | (v0.5, in progress) Deciding whether a replay fixed the run. | Is the warning light still off after the test drive? |
| **Incident** | Failed runs grouped by the same root cause. | Several cars with the same faulty part |
| **Drift** | A change in how a group of similar runs behaves over time (cost, steps, errors). | Fuel economy slowly getting worse |
| **TDN spec** | The span attributes Tracedown records, defined as a JSON Schema in `spec/`. Uses OpenTelemetry `gen_ai.*` names where they exist. | The format of the logbook |
| **Read-only mode** | `tracedown up --read-only`: every write is refused once the demo runs are loaded. Used by the public demo. | A museum car: look, don't drive |
