# Diagnose a CrewAI crew in 5 minutes

A step-by-step walkthrough: run a two-agent CrewAI crew with a deliberate bug, record it with Tracedown, and read the AI stack trace that points at the root cause.

No API key is required: the sample crew runs on a scripted stand-in model, so every run reproduces exactly.

## 1. Install

```bash
pip install crewai agent-tracedown-server
git clone https://github.com/nbn07/agent-tracedown && cd agent-tracedown   # for the sample crew
```

## 2. The crew, and its bug

The sample lives in `examples/crewai_support_crew.py`. A customer cancelled an annual plan after 10 days and asks how much they will get back. Two agents handle it in sequence:

- a **Policy researcher** with a `refund_policy` tool. The current policy (version 2026.2) pays a prorated refund of **$80**;
- a **Support writer** that replies to the customer using the researcher's findings.

The bug is in the handoff. The researcher's summary drops the $80 it just read and says the customer is owed "the full amount from the old policy" — so the writer fills the gap with the old plan's **$120**:

```
crew reply: Good news: your refund of $120 will arrive within 5 business days.
```

Nothing crashes and the crew reports success. The customer has still been promised money the policy does not pay — the kind of failure an exit code cannot catch.

The agents run on `ScriptedLLM`, a small `BaseLLM` subclass in the sample that returns canned replies (and emits CrewAI's LLM call events, like a real provider). Two things in the sample are worth copying for a real crew:

```python
from agent_tracedown.integrations import crewai as tdn_crew

listener = tdn_crew.instrument()   # one line: every kickoff is recorded
crew.kickoff()
```

## 3. Run it against a server

In one terminal, start Tracedown (server and web app):

```bash
tracedown up
```

In another, run the crew and send the run to the server:

```bash
python examples/crewai_support_crew.py --endpoint http://localhost:7070
```

The script prints the crew's reply, then the server's diagnosis. Open the run in the web app to explore it step by step. (No server? Run the script without `--endpoint` and it analyzes the run locally and prints the same stack trace.)

## 4. Read the root cause and the claims

You should see an AI stack trace similar to:

```
AgentFailureException: Unsupported claim (hallucination)
  run      41480187…  crew  2026-11-12 20:20 UTC
  at        step 6  crew → chat scripted-fake
              1 of 1 answer claims not supported by any source
  root cause: Unsupported claim (hallucination) (top suspect; check the evidence)
  evidence:   contradicted: 'Good news: your refund of $120 will arrive within 5 business days.' (states 120 but tool:refund_policy@3 (step 3) says 2026.2, 30, 80)
  next:       replay from step 6 with your fix: tdn.replay(bundle, mode="from-step", from_step=6)
```

### What each line means

- **Exception type** — the high-level diagnosis: an unsupported claim (hallucination). Tracedown checks the final answer's claims — here the refund amount — against what the run's tools and sources actually said.
- **at step 6** — the step where the claim was made: the writer's model call that produced the reply.
- **root cause** — the step Tracedown blames, with the claims verdict: 1 of 1 answer claims not supported by any source.
- **evidence** — the checkable fact: the reply states **120**, but the only policy the crew read (`tool:refund_policy` at step 3) says **80** (the other numbers are the policy's version, 2026.2, and its 30-day window). Nobody ever produced $120 from a source — the researcher's summary dropped the amount, and the writer invented it.
- **next** — a suggested replay from the broken step, e.g. after fixing the summary to carry the amount through.

In the web app the same run shows each agent, model call and tool call as a step, and the run's **claims** panel lists the reply's claim with its verdict — contradicted by step 3 — next to the evidence above.

## 5. Use it with a real crew

The instrumentation does not change: keep the one `tdn_crew.instrument()` line, swap the scripted model for a real CrewAI `LLM`, and point `tdn.init(...)` at your server (or leave the endpoint out and export wherever your other runs go). Every kickoff becomes a run with its agents, model calls, tool calls and handoffs, and failed or misleading runs get the same stack trace and claims check.
