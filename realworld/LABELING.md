# Real-world evaluation protocol (fixed before any scoring)

Written on 2026-10-30, before Tracedown was run on any of this data. Changes after the first scoring run are listed at the bottom with the reason, and the held-out numbers are reported both before and after them.

## Data

[τ-bench](https://github.com/sierra-research/tau-bench) (Sierra) `historical_trajectories/`: real conversations between an LLM agent and a simulated customer, in the airline and retail domains, with the benchmark's own expected actions for each task. Only runs with `reward < 1` are used.

| Split | Source | Use |
|---|---|---|
| **dev** | `gpt-4o-airline.json`, `gpt-4o-retail.json` | Look at failures, change detectors |
| **test** | `sonnet-35-new-airline.json`, `sonnet-35-new-retail.json` | Scored once per engine version, never inspected case by case before the report |

## Reference label (derived mechanically from τ-bench's expected actions)

*Write tools* are those that change the database or end the conversation. That is every tool except `get_*`, `list_*`, `search_*`, `find_*`, `calculate` and `think`, and it includes `transfer_to_human_agents`.

Walk the agent's write-tool calls in order, keeping the list of expected actions not yet matched:

1. If the call equals an unmatched expected action (same name; arguments equal as canonical JSON), mark that action matched and continue.
2. Otherwise this is the **first deviation**:
   - no unmatched expected action has this tool name → `called_wrong_tool`;
   - otherwise → `used_wrong_tool_argument`.
   The label step is this tool call.
3. If no call deviates but expected actions remain unmatched → `goal_partially_completed`. There is no single step; this label is scored on category only.
4. If every action matched but the reward is still below 1 (required information missing from the answer) → `missing_output`. The label step is the final assistant message.

This uses τ-bench's own fault types (`called_wrong_tool`, `used_wrong_tool_argument`, `goal_partially_completed`) plus `missing_output`. None of these come from Tracedown's own categories.

## How Tracedown's answer is scored

- **Localization@1** (cases with a label step): Tracedown's top finding points at the labelled tool call, or at the LLM turn that emitted that call (the same decision).
- **Localization@3**: the same, within the top three findings.
- **Category agreement**, using this mapping, fixed now:

| τ-bench label | Tracedown categories that count as agreeing |
|---|---|
| `called_wrong_tool` | `wrong_tool`, `planning_failure`, `unconfirmed_action`, `policy_violation` |
| `used_wrong_tool_argument` | `bad_llm_output`, `invalid_tool_args`, `ungrounded_argument` |
| `goal_partially_completed` | `incomplete_task`, `planning_failure` |
| `missing_output` | `hallucination`, `incomplete_task` |

- **Detection**: Tracedown reports any cause other than "run marked failed" (`reported_failure`).
- **Baselines**: blame the first tool error; blame the last LLM turn.

## Limits of these labels

- They are mechanical and come from expected actions. Where several action sequences would also satisfy the customer, a reasonable alternative counts as a deviation.
- Failures caused by the simulated customer (τ-bench's `user` fault author) are not separated out.
- A human spot-check of a random sample (`label_sheet.py`) measures how often the mechanical label is right. Report that agreement next to the scores.

## Changes after the first scoring

1. **Lookups in the expected actions are ignored** (found on dev, 2026-10-30). τ-bench's expected action lists include read calls such as `find_user_id_by_name_zip`. The first rule left these unmatched and labelled runs `goal_partially_completed` when every database change was right. Only expected write actions are now compared, which matches how τ-bench scores (by database state). The change is in the labels, not the engine, and applies equally to both splits.

2. **Converter fix: reused tool-call ids** (found on the first test run, 2026-10-30). The Claude 3.5 Sonnet trajectories use the id `"0"` for every tool call; GPT-4o's ids are unique. The converter keyed calls by id, so on the test split calls overwrote each other and labels pointed at the wrong step: even the first-write baseline scored 0%. Calls are now keyed by position. That first test run was invalid and is discarded; the engine was not changed between it and the reported run.

## Engine changes made on the dev split (GPT-4o), before the single test run

Starting point on dev: localization@1 **20%**; first-write-call baseline **68%**.

| # | Change | Why (dev evidence) | Generic? |
|---|---|---|---|
| E1 | User messages from later turns count as evidence for the hallucination investigator; courtesy sentences ("let me know", "I hope…") are not claims | 138 of 298 dev runs blamed "hallucination", mostly on closing pleasantries and facts the customer had stated | Yes: any multi-turn agent |
| E2 | A tool error followed by a later successful call to the same tool is "recovered" | "user not found" then success with corrected input was blamed | Yes |
| E3 | A failed *lookup* that the agent moved past is informational (confidence 0.3) | Read errors change no state | Yes |
| E4 | A refused *state-changing* call (business rule, not outage) is a planning failure; outages (5xx, timeout, unavailable) stay tool failures | "non-delivered order cannot be exchanged" means the agent chose an action whose precondition failed | Yes |
| E5 | New prior `point_of_no_return`: in a run that failed on outcome, the first attempted state-changing call is the prime suspect (confidence 0.55) | First-write baseline localizes 68% on dev | Yes, as a prior; it does not explain *why* |
| E6 | In a failed run that changed state, hallucination findings rank ×0.7 | Text findings outranked action findings | Yes |
| E7 | Keyword "request matches another tool" hint lowered to 0.45 | 38% precision on dev (61 of 160) | Yes |
| E8 | Optional LLM judge for runs whose best rule finding is weak | Remaining failures are semantic (wrong valid item or order) | Needs a model key; not used in the rules-only scores |

Tried and **not** shipped, because the dev data showed no signal:
- "ungrounded argument" (a write argument absent from earlier conversation): guilty calls 15%, innocent calls 12%;
- "no explicit confirmation before a write": guilty calls 26%, innocent calls 49%.

The synthetic benchmark (TDN-RCA-Bench holdout) stayed at 100% after these changes. E4 initially broke it (a 500 error treated as a refusal) and was fixed by the outage/refusal split.

## LLM judge run (Claude Haiku 5.5), 2026-10-30

Changes made on dev before the single test run:

- **J1, infrastructure.** Claude Haiku 5.5 rejects `temperature` and thinks before answering, and thinking counts against `max_tokens`. With the original 400-token budget, 273 of 284 dev answers came back empty. The judge now drops `temperature` when a model rejects it and allows 4,000 tokens (`TDN_JUDGE_MAX_TOKENS`). The empty-answer run was discarded.
- **E9, engine.** When the judge names a model turn where a bad plan was proposed (category `wrong_tool`, `bad_llm_output` or `policy_violation`) and a state-changing call follows, the finding is placed on that call, with "plan first proposed at step N" as evidence. On dev the judge named the earlier reasoning turn in 30 of the 43 cases where it disagreed with a correct first-write location. Dev: localization@1 64% → 71%, category agreement 56%.

The judge is asked only when the best rule finding scores below 0.8: 284 of 298 dev runs and 487 of 499 test runs.

Test split, single run (`results-test-judge-haiku-5-5.json`):

| System | loc@1 | loc@3 | Category agrees |
|---|---|---|---|
| Tracedown, rules only | 65% | 82% | 1% |
| Tracedown + judge (Claude Haiku 5.5) | 63% | 82% | 45% |

(Numbers as first reported; see change 3 below for the current ones.)
| Baseline: first write call | 74% | 74% | 29% |
| Baseline: last LLM turn | 21% | 21% | 42% |

- Category agreement by label with the judge: wrong action 53%, right action with wrong details 54%, stopped short 27%, bad final answer 7%. The last-LLM-turn baseline's 42% comes from always answering `bad_llm_output`, which matches the most common label and nothing else.
- The judge alone puts the right step first in 45% of test runs (60% on dev). Its location adds nothing over the rules; its value is naming the kind of mistake.
- Dev to test drop: the judge reads GPT-4o transcripts better than Claude 3.5 Sonnet transcripts (dev 60% vs test 45% judge-alone localization). No further changes were made after the test run.

## Live harnesses (added after the test run; not part of the reported scores)

`realworld/live/` runs real open-source agents with a model of your choice and writes case files that `score.py --file` reads. Live files also hold the passing runs; only failures are scored. Labels:

| Harness | Agent | Label rule | Category |
|---|---|---|---|
| `tau_bench_live.py` | τ-bench tool-calling agent (airline, retail) | Same as above (expected write actions) | τ-bench categories |
| `openai_agents_customer_service.py` | OpenAI Agents SDK customer-service example (triage → FAQ / seat booking, handoffs) | Same rule; the only write tool is `update_seat`; a reply missing a required fact is `missing_output` | τ-bench categories |
| `langgraph_sql_agent.py` | LangGraph `create_react_agent` with SQL tools on Chinook | Gold SQL value. Not in any query result → last `run_query`; retrieved but not reported → final model turn | `wrong_answer` |

`wrong_answer` agrees with `bad_llm_output`, `hallucination`, `invalid_tool_args`, `ungrounded_argument` and `judged`. The SQL label is a heuristic: check a sample by hand before quoting numbers from it.

Each harness has `--fake`, a scripted model with deliberate bugs, so the plumbing can be checked without a key. Fake-model scores test the harness, not Tracedown, and are not reported.

Building the harnesses found two bugs in the OpenAI Agents SDK adapter, both fixed: tool arguments were not recorded (the SDK fills them in after the span starts), and handoff targets showed as `None` (only known when the handoff resolves). The τ-bench numbers above do not use that adapter and are unaffected.

First live results (Claude Haiku 5.5) are in `README.md`. Runs that ended in an API error (rate limits on the first attempt) were discarded and rerun with retries; they are never counted as agent failures.

## Change 3: claim splitter bug (2026-10-30, after the test runs)

Found while building the Langfuse/OpenTelemetry importer, on a hand-written example rather than on τ-bench or Who&When data. The hallucination investigator split sentences at every period, so "$4.2 billion" or "example.com" became fragments that no source supported, producing false "unsupported claim" findings. A sentence now ends only at a period followed by whitespace or the end of the text. Nothing else changed.

Effect, reported both ways:

| | Before | After |
|---|---|---|
| τ-bench test, rules only (loc@1 / loc@3) | 65% / 82% | 68% / 81% |
| τ-bench dev, rules only | 67% / 83% | 68% / 84% |
| τ-bench test, + judge (cached answers) | 63% / 82% | 63% / 81% |
| Who&When Hand-Crafted (agent / step) | 34.5% / 10.3% | 51.7% / 19.0% |
| Who&When Algorithm-Generated | 50.8% / 21.4% | unchanged |
| Synthetic holdout | 100% | 100% |

The judge's answers were reused from the cache, so its prompts list the old rule candidates. The first-write baseline (74% on test) still beats the rules at top-1.

## Label check by a second AI (Microsoft Copilot), 2026-10-30

The 50-run sample from `label_sheet.py` (test split, seed 7) was checked with labels **suggested by Microsoft Copilot**. They were not reviewed by a person, so this is a second AI's opinion, not human judgement. The labels are in `label-check/copilot-labels-50.csv`; rescore with `python realworld/label-check/score_check.py`.

| Copilot's verdict on the reference label | Runs |
|---|---|
| Right | 25 |
| Wrong | 23 (12 "the simulated customer caused it", 7 "something else", mostly no real agent error) |
| Not sure | 2 |

The reference label agreed with Copilot in **52%** of decided runs. In about 4 of 10 sampled "agent failures", Copilot found no agent mistake: the τ-bench simulated customer gave wrong details, refused an offer or chose another item. Every τ-bench number in this file is measured against that noisy reference.

On the 31 runs with a Copilot-checked step:

| | Right step |
|---|---|
| Reference label | 58% |
| Tracedown rules, top 1 / top 3 | 48% / 58% |
| Tracedown + judge, top 1 / top 3 | 45% / 58% |
| Baseline: first write call | 48% |

The sample is small, so these differences are within noise. Two conclusions:
- Report τ-bench numbers only with this caveat; Who&When (human labels) is the stronger benchmark.
- A useful product feature follows: telling "the agent made a mistake" apart from "the user caused it" and "nothing went wrong".
