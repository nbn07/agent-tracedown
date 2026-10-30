# TDN-RCA-Bench v0

A public benchmark for **root-cause analysis of failed AI agent runs**: given a recorded run, name the cause and the step where it happened.

Each case is one recorded run (spans and payloads, in the same bundle format as `GET /api/v1/runs/{id}/bundle`) with a label:

```json
{"id": "holdout-stale_source-03", "family": "stale_source", "distractor": true,
 "label": {"category": "stale_source", "step": 2, "span_name": "retrieval"},
 "bundle": {"spans": [...], "blobs": {...}}}
```

## Failure families

| Family | What went wrong | Where the label points |
|---|---|---|
| `bad_llm_output` | Model extracted an unusable value; a tool rejected it | the LLM step, not the tool that crashed |
| `wrong_tool` | Agent called the wrong tool for the request | the tool call |
| `stale_source` | Answer came from a superseded document | the retrieval |
| `retrieval_failure` | Retrieval returned nothing or nothing relevant | the retrieval |
| `loop` | Same tool, same arguments, again and again | the first repeated call |
| `context_window` | Prompt filled the context window | the LLM step |
| `hallucination` | Answer contradicts a tool result | the LLM step that wrote the answer |
| `memory_corruption` | Memory read returned a different value than was written | the read |
| `planning_failure` | Agent chose an option whose precondition had failed | the decision |
| `tool_failure` | External service error with valid arguments | the tool call |

Two thirds of cases carry a **distractor**: a tool error that recovered on retry, or a weak retrieval whose chunks were never used. "Blame the first error" fails on these.

## Splits

| Split | Cases | Use |
|---|---|---|
| `dev` | 80 | Development. Tracedown was tuned on it |
| `test` | 120 | Different seed and wordings. Scored once, then used to fix the misses it revealed |
| `holdout` | 100 | New wordings and number formats, written after all fixes. Never used for tuning. **Report this one** |

```bash
python benchmark/generate.py                 # regenerate data/ (deterministic)
python benchmark/score.py --split holdout    # Tracedown and two baselines
```

## Results (Tracedown v0.2, October 2026)

| System | Split | category@1 | exact@1 | exact@3 |
|---|---|---|---|---|
| Tracedown | test, **before fixes** (held out at the time) | 93% | 93% | 93% |
| Tracedown | holdout | 100% | 100% | 100% |
| Baseline: first error span | holdout | 10% | 10% | 10% |
| Baseline: last LLM call | holdout | 10% | 10% | 10% |

*exact* = right category **and** right step.

The first run on `test` missed 8 of 120 cases. Each miss was a real gap, and each was fixed:

- Short structured values such as `"Q3"` were not linked between steps.
- A number in the answer was accepted if it appeared in *any* source, e.g. "9" in "Office hours are 9 to 5".

Fixes made against `dev` before that:

- A 500 error from a tool with valid arguments was blamed on the model.
- A tool error that succeeded on retry was treated as the cause.
- An unused low-relevance retrieval outranked the real cause.

## Read these numbers carefully

- **Synthetic and in-distribution.** The splits change wordings, seeds, values and distractors, but share the same ten scenario structures. That was the engine's development set. 100% on `holdout` shows robustness to surface variation, not to failure shapes the engine has never seen.
- **The baselines are deliberately simple.** They stand for what a trace viewer alone gives you (the first red span, or "the model got it wrong"). They also cannot output most categories by design.
- **The next version needs real runs.** v1 of the benchmark should be mostly real, redacted failing runs from production agents, labelled by the people who debugged them, with scenario types the engine was not built around.

## Contributing cases

Export a failing run with `GET /api/v1/runs/{id}/bundle` and redact it. The server's PII policy can do this. Add a label line and open a PR. Label the step where the cause *entered* the run, not where it surfaced. If two causes are needed together, label the earlier one and describe both in `notes`.

## Submitting a system

Implement `bundle -> [{"category", "step"}, ...]` (ranked) and add it to `SYSTEMS` in `score.py`. Category names are the ten above. Systems that use an LLM should report model, prompt and cost per case.
