# Proposal: GenAI decision spans, replay provenance, and memory consistency fields

**For:** OpenTelemetry Semantic Conventions, GenAI SIG (`open-telemetry/semantic-conventions-genai`)
**From:** Tracedown maintainers
**Status:** Draft for discussion, not yet filed (October 2026)
**Reference implementation:** Tracedown SDK and server (`tdn.*` attributes today), Apache-2.0

## Summary

Agent traces today show *what* ran: `invoke_agent`, `chat`, `execute_tool`, `retrieval`. Debugging a failed agent run also needs *why a path was taken* and *whether a re-run is comparable to the original*. We propose three small additions:

1. **Decision spans** (`gen_ai.decision.*`): an explicit record of a choice the agent made, such as a route, a plan step or a tool selection, with the options it had and whether the choice's precondition held. **New.**
2. **Replay provenance** (`gen_ai.replay.*` plus content digests): mark a trace as a replay of another and say, per step, whether the output was served from the recording or produced live. **New.** It complements the in-flight checkpoint/resume work (#159, PR #445), which covers *continuing* a run, not *re-running* one.
3. **Memory consistency fields**: two fields to add to the existing memory proposal (#200), not a competing namespace.

Everything below is implemented and in use under `tdn.*` names. We would rename to whatever the SIG prefers and emit both during the transition.

## Prior work we build on

| Work | What it covers | How this proposal relates |
|---|---|---|
| [GenAI agent spans](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans) (Development) | `create_agent`, `invoke_agent`, `execute_tool`; agent and tool attributes | Decision and replay attributes attach to these spans; no new span kinds for tools or agents |
| [#2664 Agentic systems meta-issue](https://github.com/open-telemetry/semantic-conventions/issues/2664) | Tasks, actions, agents, teams, artifacts, memory | Planning is only an agent capability there; decision spans fill that gap at the action level |
| [#200 Memory systems](https://github.com/open-telemetry/semantic-conventions-genai/issues/200) (and PR #140) | Memory store/search/update/delete operations, scope, layer | We adopt it as is and propose two fields (section 3) |
| [#462 Durable execution](https://github.com/open-telemetry/semantic-conventions-genai/issues/462), #159, PR #445 | Logical execution id across suspend/resume, checkpoint and resume events | Replay reuses the span-link pattern and the execution id; replay is a new run, not a continuation |

## 1. Decision spans

### Why

Most agent failures we diagnose are wrong choices, not crashes: the router picked `refund_order` for a cancellation request, or the planner booked a meeting although the "free slot found" check failed. Today the choice is visible only indirectly (which tool span follows). Root-cause tools have to guess the decision point; an explicit span removes the guess.

### Span

- `gen_ai.operation.name` = `decide` (new well-known value)
- Span name: `decide {gen_ai.decision.type}`, e.g. `decide tool_selection`
- Span kind: `INTERNAL`
- Parent: the `invoke_agent` span making the choice

### Attributes

| Attribute | Type | Requirement | Description | Example |
|---|---|---|---|---|
| `gen_ai.decision.type` | string | Required | What kind of choice. Well-known: `tool_selection`, `route`, `plan_step`, `handoff`, `stop`, `guardrail` | `route` |
| `gen_ai.decision.options` | string[] | Recommended | Options available at the time | `["create_event", "find_free_slot", "ask_user"]` |
| `gen_ai.decision.chosen` | string | Required | The option taken | `create_event` |
| `gen_ai.decision.precondition.satisfied` | boolean | Conditionally required: if the application checks a precondition | Whether the chosen option's precondition held | `false` |
| `gen_ai.decision.rationale` | string | Opt-in (may contain sensitive content) | Model- or code-provided reason | `user gave a day and a time` |
| `gen_ai.decision.confidence` | double | Opt-in | Score the decider reported, if any, in [0, 1] | `0.62` |

A guardrail check is a decision with `type=guardrail`, `options=["pass","tripwire"]`. This lets the OpenAI Agents SDK guardrail spans map directly.

### What it enables

- "Chose X although its precondition failed" is a span lookup, not a heuristic.
- Drift reports can compare how often each option is chosen before and after a prompt change.

## 2. Replay provenance

### Why

Teams re-run failed agent traces with one change: a new prompt, another model, a fixed tool. A fair comparison needs (a) a link from the new trace to the original and (b) for each step, whether its output was **recorded** (fed back from the original) or **live**. Without (b), a diff mixes "the model behaved differently" with "the world changed".

### Attributes on the root `invoke_agent` span of the replayed trace

| Attribute | Type | Requirement | Description | Example |
|---|---|---|---|---|
| `gen_ai.replay.source.trace_id` | string | Required | Trace being replayed | `4bf92f3577b34da6a3ce929d0e0e4736` |
| `gen_ai.replay.mode` | string | Required | `exact` (all recorded), `from_step` (recorded before N, live after), `swap` (model calls live, others recorded), `full` (all live) | `from_step` |
| `gen_ai.replay.from_step` | int | Conditionally required: if mode is `from_step` | First live step | `4` |
| `gen_ai.replay.overrides` | string[] | Recommended | What was changed | `["gen_ai.request.model", "prompt"]` |
| `gen_ai.replay.first_divergence` | int | Recommended | First step whose input differed from the recording | `5` |

The root span also carries a **span link** to the original root span, with link attribute `gen_ai.replay.relation = "replay_of"`, following the link pattern from #462.

### Attributes on each step span (`chat`, `execute_tool`, `retrieval`, memory operations)

| Attribute | Type | Requirement | Description |
|---|---|---|---|
| `gen_ai.step.index` | int | Recommended | Order of the step within the run (0 = root). Lets two traces be aligned step by step |
| `gen_ai.replay.output.source` | string | Required on replayed traces | `recorded`, `live`, or `live_diverged` (meant to be recorded, but its input changed upstream) |
| `gen_ai.input.digest` | string | Opt-in | `sha256:<hex>` of the canonical JSON of the step's input |
| `gen_ai.output.digest` | string | Opt-in | Same for the output |

**Digests** let backends deduplicate payloads (the same system prompt in a million spans), match a replayed step to its recording, and detect divergence, all without putting content on the span. The content itself stays out of band, consistent with the SIG's content-capture guidance.

### Canonical form for digests

UTF-8 JSON with sorted object keys, no insignificant whitespace, numbers as emitted by the language's standard JSON encoder. The digest identifies the payload *as captured*. If a redaction policy rewrites content before storage, the digest still names the original.

## 3. Memory: two fields for #200

We adopt the operations and attributes proposed in #200. Two additions came out of real failures we debugged:

| Attribute | Type | Requirement | Description |
|---|---|---|---|
| `gen_ai.memory.value.digest` | string | Recommended | Digest of the value written or read. A read whose digest differs from the last write to the same key in the same scope is a consistency failure, visible without capturing content |
| `gen_ai.memory.version` | string | Opt-in | Store-provided version or etag, when the backend has one |

This agrees with the review comment on #200 that opaque ids and store/search/update/delete are the portable core. Both fields are opaque.

## Compatibility and rollout

- Tracedown emits `tdn.decision.*`, `tdn.replay.*`, `tdn.memory.value_digest`, `tdn.step` today. On acceptance it emits the `gen_ai.*` names as well, behind `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`, which the GenAI conventions already use for their migration.
- Mapping from current names:

| Current (`tdn.*`) | Proposed |
|---|---|
| `tdn.decision.options` / `.chosen` / `.rationale` | `gen_ai.decision.options` / `.chosen` / `.rationale` |
| `tdn.decision.precondition_ok` | `gen_ai.decision.precondition.satisfied` |
| `tdn.replay.of` / `.mode` / `.first_divergence` | `gen_ai.replay.source.trace_id` / `.mode` / `.first_divergence` |
| `tdn.replay.source` | `gen_ai.replay.output.source` |
| `tdn.step` | `gen_ai.step.index` |
| span `input_digest` / `output_digest` | `gen_ai.input.digest` / `gen_ai.output.digest` |
| `tdn.memory.value_digest` | `gen_ai.memory.value.digest` |

## Open questions for the SIG

1. Should `decide` be a new operation, or an event on `invoke_agent`? We chose a span so it can have a duration (an LLM router call) and children.
2. Is `gen_ai.step.index` better as a span link order, or does #462's execution id plus a sequence number already serve?
3. Should digests live in the GenAI namespace or in a general content-reference convention shared with HTTP bodies and messaging payloads?

## Next steps

1. Open a discussion issue in `semantic-conventions-genai` linking #2664, #200 and #462, with this document.
2. Offer a PR against the agent spans document for `decide` and the replay attributes once the naming discussion settles.
3. Comment on #200 with the two memory fields.
