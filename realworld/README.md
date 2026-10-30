# Real-world evaluation

The synthetic benchmark (`benchmark/`) tells you whether Tracedown finds failures it was built to find. This folder asks the harder question: on real agent failures, does it point at the right step?

## 1. τ-bench historical failures (no API key needed)

[τ-bench](https://github.com/sierra-research/tau-bench) publishes full trajectories of real models on airline and retail customer-service tasks. Each failed trajectory becomes a Tracedown trace with a mechanical root-cause label. The protocol, including every change made after the first scoring, is fixed in [LABELING.md](LABELING.md).

```bash
git clone https://github.com/sierra-research/tau-bench ../tau-bench && pip install -e ../tau-bench
python realworld/tau_bench_import.py --tau-bench ../tau-bench     # dev = GPT-4o, test = Claude 3.5 Sonnet
python realworld/score.py --split dev                              # tune here
python realworld/score.py --split test                             # report this, once
python realworld/score.py --split test --judge openai:gpt-4.1-mini # adds the LLM judge (needs a key)
```

Held-out test split, 499 failed runs (383 with a single cause step), rules only:

| System | loc@1 | loc@3 | Category agrees |
|---|---|---|---|
| **Tracedown** | **68%** | **81%** | 1% |
| Baseline: first write call | 74% | 74% | 29% |
| Baseline: last write call | 55% | 55% | 29% |
| Baseline: last LLM turn | 21% | 21% | 42% |
| Baseline: first tool error | 13% | 13% | 0% |

What this says: without a model, Tracedown has the right step in its top three 81% of the time, but its first guess is worse than "blame the first state-changing call", and it almost never names the *kind* of mistake. These failures are semantic (the right action with the wrong order or item), which rules cannot see.

**With the LLM judge** (Claude Haiku 5.5, one run on test after tuning on dev; details in [LABELING.md](LABELING.md)):

| System | loc@1 | loc@3 | Category agrees |
|---|---|---|---|
| Tracedown + judge | 63% | 81% | **45%** |
| Tracedown, rules only | 68% | 81% | 1% |
| Baseline: first write call | 74% | 74% | 29% |
| Baseline: last LLM turn | 21% | 21% | 42% (always the same answer) |

The judge names the right kind of mistake for about half of wrong actions and wrong details, which rules almost never do. It does not find the step better: on its own it puts the right step first in 45% of test runs. The judge's 771 answers (dev and test) are in `realworld/judge-answers-haiku-5-5.jsonl`; copy that file to `realworld/data/judge-cache-anthropic_claude-haiku-5-5.jsonl` to recompute the scores without any API calls.

**Human check.** `python realworld/label_sheet.py --split test --n 50` writes a sheet and readable transcripts to `realworld/labeling/`. Fill in the `human_*` columns, then `--score realworld/labeling/sheet.csv` reports how often the mechanical label is right and how Tracedown does against human labels.

## 1b. Who&When (public failure-attribution benchmark)

184 multi-agent failure logs with labelled culprit agent and step. Rules only, exact match: step accuracy 21% (algorithm-generated) and 19% (hand-crafted). That is in the range of the paper's GPT-4o judges, and on the hand-crafted set above them and above a "first failed output" baseline (23% / 16%), though not on the algorithm-generated set. Details and caveats: [whowhen/README.md](whowhen/README.md).

## 2. Live harnesses around real agent projects (your model, your key)

| Harness | Project | What it checks |
|---|---|---|
| [`live/tau_bench_live.py`](live/tau_bench_live.py) | sierra-research/tau-bench | Runs τ-bench live with any LiteLLM model, converts its results with the same rules |
| [`live/openai_agents_customer_service.py`](live/openai_agents_customer_service.py) | openai/openai-agents-python `examples/customer_service` | 12 scripted customer conversations (seat changes, FAQ, out-of-scope requests), traced through the Agents SDK adapter |
| [`live/langgraph_sql_agent.py`](live/langgraph_sql_agent.py) | LangGraph `create_react_agent` + Chinook database | 12 questions with gold SQL answers, traced through the LangChain/LangGraph adapter |

```bash
export OPENAI_API_KEY=...
python realworld/live/openai_agents_customer_service.py --agents-repo ../openai-agents-python --model gpt-4.1-mini --repeats 3
python realworld/live/langgraph_sql_agent.py --db ../chinook-database/ChinookDatabase/DataSources/Chinook_Sqlite.sqlite --model openai:gpt-4.1-mini --repeats 3
python realworld/live/tau_bench_live.py --tau-bench ../tau-bench --env retail --model gpt-4.1-mini --tasks 0-19
python realworld/score.py --file realworld/data/live-oa-cs-gpt-4.1-mini.jsonl
```

**First live run (2026-10-30, Claude Haiku 5.5 as the agent and, for τ-bench, as the simulated customer):**

| Harness | Runs | Failed | Tracedown loc@1 / loc@3 | + judge loc@1 / category | First-write baseline |
|---|---|---|---|---|---|
| τ-bench live (40 retail + 25 airline tasks) | 65 | 25 | 64% / 86% | 55% / 40% | 73% (category 52%) |
| OpenAI Agents SDK customer service (12 conversations × 3) | 36 | 3 | 100% / 100% | 67% / 0% | n/a (no writes) |
| LangGraph SQL agent (21 questions × 2) | 42 | 0 | n/a | n/a | n/a |

- τ-bench live agrees with the historical test split: the rules find the step's neighbourhood (86% top-3) but the first-write prior wins top-1, and on these 25 failures the judge did not help.
- All three customer-service failures are the same: "Which rows are the exit rows?" The example's `faq_lookup_tool` matches keywords and has no answer for that wording, so the agent correctly says it does not know. The mechanical label blames the final reply; the real cause is the tool. Tracedown's "unsupported claim" on the reply matches the label but not the truth.
- The SQL agent answered every question correctly, including nine harder ones added after the first run. A stronger test needs harder or ambiguous questions, or a weaker model.

Add `--endpoint http://localhost:8000` to also send every run to a Tracedown server and look at the failures in the UI. Add `--fake` to check a harness without a key: it swaps in a scripted model with deliberate bugs. Fake-model scores test the plumbing only.

Case files go to `realworld/data/` (gitignored). Each line is one run: `success`, `label` (for failures) and the full trace `bundle`.
