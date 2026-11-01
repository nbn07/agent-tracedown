# Accuracy and Benchmarks

A diagnosis tool is only useful if you know how often to trust it. Tracedown publishes its results, including where it loses.

## The headline, in plain words

On hard, real multi-agent failures, **Tracedown's first guess is wrong more often than it is right.** It is still useful: it narrows a run of dozens of steps down to a short list of suspects with evidence, and the right step is often in the top three. Think of a doctor's shortlist of likely causes, not a lab result.

## Who&When (184 real multi-agent failures, labelled by people)

[Who&When](https://github.com/mingyin1/Agents_Failure_Attribution) asks: which agent made the decisive mistake, and at which step? It has two sets: *algorithm-generated* (126 runs) and *hand-crafted* (58 runs, harder).

| Setup | Right step, first guess | Right step in top three |
|---|---|---|
| With a Claude Sonnet 5.5 judge | 42.9% / 24.1% | 58.7% / 36.2% |
| With a Claude Haiku 5.5 judge | 36.5% / 20.7% | 50.8% / 32.8% |
| Rules only (no LLM) | 21.4% / 19.0% | 33.3% / 31.0% |

The two numbers in each cell are algorithm-generated / hand-crafted. On the stricter "right agent **and** right step" measure, Tracedown with Sonnet scores 36.4%, ahead of AgentDebugX at 28.8% (with Qwen 3.5 9B). The models differ, so this compares setups, not methods.

## τ-bench (customer-service agents, test split, rules only)

| | First guess right | Top three |
|---|---|---|
| Tracedown | 74% | 81% |
| "Blame the first change to the database" | 74% | — |

Honest reading: on these runs, Tracedown's first guess ties a simple rule of thumb. Most of its correct first guesses come from that same idea (the point-of-no-return detector). The extra value is in the top three and the evidence.

## Is a high score more trustworthy?

We checked. **No.** Runs where the top finding scored 0.8 or more were not reliably more often right. That is why the app shows "top suspect" and an ordered list instead of percentages. Details: [How Tracedown decides → Rank](https://nbn07.github.io/agent-tracedown/HOW-IT-DECIDES/#4-rank).

## Run the benchmarks yourself

```bash
# Who&When (needs a clone of the dataset next to this repo)
python realworld/whowhen/convert.py --repo ../Agents_Failure_Attribution
python realworld/whowhen/score.py --json realworld/whowhen/results.json
python realworld/whowhen/score.py --judge anthropic:claude-sonnet-5-5   # rescores from saved answers, no API calls

# τ-bench
python realworld/tau_bench_import.py --tau-bench ../tau-bench
python realworld/score.py --split test

# Synthetic benchmark (TDN-RCA-Bench)
python benchmark/generate.py
python benchmark/score.py --split holdout
```

To run a judge with new calls, use the **judge-benchmark** workflow in the Actions tab. It reads the API key from the repository secrets, so no key is ever pasted anywhere.

**Rule for changes:** a pull request that touches `server/tracedown_server/rca/` should say whether these numbers moved. A detector that helps one case but lowers Who&When or τ-bench needs a good reason.

## Sources

- [`realworld/whowhen/README.md`](https://github.com/nbn07/agent-tracedown/blob/main/realworld/whowhen/README.md): full tables, baselines, method
- [`realworld/README.md`](https://github.com/nbn07/agent-tracedown/blob/main/realworld/README.md): τ-bench and live harnesses
- [`realworld/LABELING.md`](https://github.com/nbn07/agent-tracedown/blob/main/realworld/LABELING.md): how labels were checked
- [Benchmarks page](https://nbn07.github.io/agent-tracedown/benchmarks/) on the documentation site
