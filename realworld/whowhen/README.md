# Who&When: which agent, and which step, caused the failure?

[Who&When](https://github.com/mingyin1/Agents_Failure_Attribution) (Zhang et al., ICML 2025) is the standard public benchmark for failure attribution in multi-agent systems. It has 184 failed runs, each labelled with the agent and the step (0-based history index) where the decisive mistake happened:

- **Algorithm-Generated:** 126 logs from CaptainAgent teams, about 9 steps each.
- **Hand-Crafted:** 58 logs from Magentic-One, about 52 steps each.

```bash
git clone https://github.com/mingyin1/Agents_Failure_Attribution ../Agents_Failure_Attribution
python realworld/whowhen/convert.py --repo ../Agents_Failure_Attribution
python realworld/whowhen/score.py --json realworld/whowhen/results.json
```

## Method

`convert.py` turns each log into a Tracedown trace. Each history entry becomes spans tagged with its Who&When index and agent:

- Agent messages become LLM spans.
- Code-execution output becomes tool spans, marked failed when the output shows an error.
- WebSurfer and FileSurfer turns become tool spans: each reports an action.
- Both tools are declared read-only.

Tracedown runs **rules only, with no LLM and no tuning on this data**. When Tracedown blames code output, the prediction is the message and agent that wrote the code, which is how Who&When labels it. Scoring is exact match on agent and on step. The official `evaluate.py` counts a step as correct when the label is a substring of the prediction ("1" matches "12"), which is more lenient.

## Results

Ground truth makes no difference to the rules, so one table covers both settings.

| | Alg. agent | Alg. step | Alg. step@3 | Hand agent | Hand step | Hand step@3 |
|---|---|---|---|---|---|---|
| **Tracedown (rules only)** | **50.8%** | **21.4%** | 33.3% | **51.7%** | **19.0%** | 31.0% |
| Baseline: first failure-looking output | 50.0% | 23.0% | 23.0% | 58.6% | 15.5% | 15.5% |
| Baseline: last step | 44.4% | 0.8% | 0.8% | 60.3% | 12.1% | 12.1% |
| Baseline: first agent turn | 49.2% | 15.9% | 15.9% | 31.0% | 1.7% | 1.7% |
| Random (expected, ours) | 39.4% | 12.0% | 36.0% | 42.5% | 4.2% | 12.5% |

The paper's own results (Table 1, GPT-4o judge) are below, as published. The paper's random agent baseline uses a different agent count than ours.

| | Alg. agent | Alg. step | Hand agent | Hand step |
|---|---|---|---|---|
| All-at-once, with GT | 54.3% | 12.5% | 55.2% | 5.3% |
| Step-by-step, with GT | 35.2% | 25.5% | 34.5% | 7.0% |
| Binary search, with GT | 44.1% | 24.0% | 51.7% | 6.9% |
| All-at-once, without GT | 51.1% | 13.5% | 53.4% | 3.5% |
| Step-by-step, without GT | 26.0% | 15.3% | 32.8% | 8.8% |
| Binary search, without GT | 30.1% | 16.6% | 36.2% | 6.9% |

### All 184 runs, agent and step both right

Some papers report one "strict" number: the run counts only when the predicted agent **and** step are both right, over all 184 runs. `score.py` now prints it as `agent+step`.

| | Agent + step (strict) | Agent |
|---|---|---|
| **Tracedown + judge (Claude Sonnet 5.5), without ground truth** | **36.4%** | **60.3%** |
| Tracedown + judge (Claude Sonnet 5.5), with ground truth | 37.5% | 62.0% |
| Tracedown + judge (Claude Haiku 5.5), without ground truth | 31.5% | 56.5% |
| Tracedown + judge (Claude Haiku 5.5), with ground truth | 34.8% | 60.3% |
| Tracedown, rules only | 20.7% | 51.1% |
| Baseline: first failure-looking output | 20.7% | 52.7% |
| AgentDebugX DeepDebug, Qwen 3.5 9B ([paper](https://arxiv.org/abs/2607.18754), as published) | 28.8% | 56.0% |
| AgentDebugX best single-pass baseline (as published) | 21.7% | 47.8% |

The judge models differ (a hosted Claude model versus a small local Qwen model), so this is not a like-for-like comparison of the methods.

## What it says

- **Algorithm-Generated:** the rules alone are in the range of GPT-4o judges at step level (21% vs 13–26%) and agent level (51% vs 26–54%), at no model cost. They do not beat the simple "first failed code output" baseline (23%).
- **Hand-Crafted:** with long Magentic-One logs, the rules beat the paper's GPT-4o methods and every simple baseline at step level (19.0% vs 3.5–8.8% and 15.5%). At agent level they are in the GPT-4o range (51.7% vs 33–55%) but below "last step" (60.3%).
- **Sensitivity.** The result depends strongly on whether browsing actions count as state-changing.
  - In a first run, a bug dropped the "read-only" declarations, so browsing counted as state-changing.
  - On Hand-Crafted, that run scored 62.1% agent and 22.4% step, because the first-action prior then points at WebSurfer's first click.
  - We report the run that matches the declared intent (read-only); the other run is not a result.

## Changes after the first scoring

1. **SDK bug.** `ManualTrace.declare_tools` dropped the `side_effects` flag, so every tool counted as state-changing. Fixed in the SDK; it affects any user who declares read-only tools.
2. **Mapping.** Code-output findings now map to the message that wrote the code, not the output itself. The agent was already mapped that way, so the step was inconsistent.

First run, before both fixes:

| | Alg. agent | Alg. step | Hand agent | Hand step |
|---|---|---|---|---|
| Tracedown | 57.9% | 2.4% | 62.1% | 22.4% |

## Caveats

- **AgentDebugX** reports about 29% exact agent-and-step accuracy (Qwen3.5-9B, multi-turn investigation). Our joint agent-and-step accuracy is 21.4% (Algorithm-Generated) and 19.0% (Hand-Crafted), against 23.0% and 15.5% for the first-failure baseline. Their setting and scoring may differ, so treat the comparison as indicative only.
- **Mistake steps count every history entry,** including code output and the human request; the converter keeps that index exactly.
- **The LLM judge was not run here,** because no API key was available.

3. **Claim splitter bug (found 2026-10-30 while building the Langfuse/OTel importer, not on this data).** The hallucination investigator split sentences at every period, so "$4.2 billion" or "example.com" became two bogus claims that no source supported. Hand-Crafted logs are full of URLs and decimals, so false "unsupported claim" findings outranked the real suspects. After the fix, Hand-Crafted went from 34.5% agent / 10.3% step to 51.7% / 19.0%; Algorithm-Generated is unchanged. The fix is generic (a sentence ends only at a period followed by whitespace) and no rule was tuned on Who&When.

## With the LLM judge (Claude Haiku 5.5), 2026-10-30

One run, no tuning on Who&When. The judge is asked only when the best rule finding scores below 0.8: 66 of 126 Algorithm-Generated logs and 5 of 58 Hand-Crafted logs. Its answers are in `judge-answers-haiku-5-5.jsonl`; `python realworld/whowhen/score.py --judge anthropic:claude-haiku-5-5` rescores from them without new calls.

| | Alg. agent | Alg. step | Hand agent | Hand step |
|---|---|---|---|---|
| **Tracedown + judge, without ground truth** | **57.9%** | **36.5%** | 53.4% | **20.7%** |
| Tracedown + judge, with ground truth | 62.7% | 40.5% | 55.2% | 24.1% |
| Tracedown, rules only | 50.8% | 21.4% | 51.7% | 19.0% |
| Baseline: first failure-looking output | 50.0% | 23.0% | 58.6% | 15.5% |
| Paper's GPT-4o methods, without ground truth (best in each column) | 51.1% | 16.6% | 53.4% | 8.8% |

- **Joint agent-and-step accuracy** (the metric AgentDebugX reports, about 29% with a 9B model) is 36.5% on Algorithm-Generated and 20.7% on Hand-Crafted without ground truth. The settings differ, so this is indicative only.
- **Hand-Crafted barely changes** because the rules rarely hand over to the judge there. Lowering that threshold would be a change to test on other data first.

## With a stronger judge (Claude Sonnet 5.5), 2026-11-01

The same engine and prompt, one run, no tuning on Who&When, run by the `judge-benchmark` workflow (Actions). Its 142 answers are in `judge-answers-sonnet-5-5.jsonl`; `python realworld/whowhen/score.py --judge anthropic:claude-sonnet-5-5` rescores from them without new calls. Full results: `results-judge-sonnet-5-5.json`. The run cost under one US dollar.

| Without ground truth | Alg. agent | Alg. step | Alg. top 3 | Hand agent | Hand step | Hand top 3 |
|---|---|---|---|---|---|---|
| **Tracedown + Sonnet 5.5 judge** | **62.7%** | **42.9%** | **58.7%** | **55.2%** | **24.1%** | **36.2%** |
| Tracedown + Haiku 5.5 judge | 57.9% | 36.5% | 50.8% | 53.4% | 20.7% | 32.8% |
| Tracedown, rules only | 50.8% | 21.4% | 33.3% | 51.7% | 19.0% | 31.0% |

- **A stronger judge helps on Algorithm-Generated** (+6.4 points on the first guess, +7.9 in the top three), where the judge is asked in about half the logs.
- **Hand-Crafted moves little** (+3.4 points) for the same reason as before: the rules answer with a high score in most logs, mostly from the loop detector, and the judge is rarely consulted. Making the judge see more of these logs is the next thing to test, on data other than Who&When.
- **Strict agent-and-step, all 184 logs:** 36.4% with Sonnet 5.5, 31.5% with Haiku 5.5, against 28.8% published for AgentDebugX with Qwen 3.5 9B. Different models, so not a like-for-like comparison of the methods.
