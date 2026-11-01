# Benchmarks

Tracedown publishes how often it finds the right step on public benchmarks, including where simple baselines do as well or better. Every script and saved judge answer is in the repository, so anyone can re-score without an API key.

## Who&When

[Who&When](https://github.com/nbn07/agent-tracedown/blob/main/realworld/whowhen/README.md) (ICML 2025) asks which step caused each of 184 failed multi-agent runs, labelled by people. Exact match, one run, no tuning on this data. The two numbers are the algorithm-generated and hand-crafted sets.

| Right step | First guess | In top three |
|---|---|---|
| **Tracedown with a Claude Sonnet 5.5 judge** | **42.9% · 24.1%** | **58.7% · 36.2%** |
| Tracedown with a Claude Haiku 5.5 judge (cheaper) | 36.5% · 20.7% | 50.8% · 32.8% |
| Tracedown, rules only | 21.4% · 19.0% | 33.3% · 31.0% |
| Baseline: first failure-looking output | 23.0% · 15.5% | – |
| Baseline: random step | 12.0% · 4.2% | 36.0% · 12.5% |
| Best GPT-4o method in the paper | 16.6% · 8.8% | – |

The first guess is wrong more often than it is right. On the algorithm-generated set, rules alone are no better than a random pick within the top three; the judge is what lifts it.

A stronger judge helps most on the algorithm-generated set. On the hand-crafted set the rules answer confidently in most logs, so the judge is rarely asked; that is the next thing to improve.

Scored the strict way some papers use (agent and step both right, all 184 runs), Tracedown reaches 36.4% with a Claude Sonnet 5.5 judge and 31.5% with Claude Haiku 5.5, and AgentDebugX with Qwen 3.5 9B reaches 28.8%. The models differ, so this compares setups, not methods.

## τ-bench

On 499 failed τ-bench customer-service runs (held-out test split, labels derived mechanically), rules only:

| | First guess | In top three |
|---|---|---|
| Tracedown | 74% | 81% |
| Baseline: blame the first database change | 74% | 74% |

Tracedown's first guess ties the simple rule. Before a fix found on the development split, it lost 68% to 74%. A second AI's check of the labels agreed with them only about half the time, so treat these numbers as approximate. The protocol and every change are in [LABELING.md](https://github.com/nbn07/agent-tracedown/blob/main/realworld/LABELING.md).

## Help make these numbers better

If Tracedown blames the wrong step on one of your runs, please [report it](https://github.com/nbn07/agent-tracedown/issues/new?template=wrong-diagnosis.yml). Every report becomes a test case for later versions.
