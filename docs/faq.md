# Frequently Asked Questions

## How is Tracedown different from Langfuse, LangSmith, or Arize Phoenix?

Those tools mainly help you collect and inspect traces. Tracedown focuses on finding the likely root cause of a failed run and showing the evidence behind it. It works alongside tracing tools rather than replacing them.

See the [comparison](https://github.com/nbn07/agent-tracedown#comparison) for more details.

## How is it different from AgentDebugX?

AgentDebugX is one of the closest alternatives. Both tools investigate agent failures and support replay. Tracedown starts with rule-based analysis, can work without an LLM, and publishes its benchmark results and limitations.

See the [comparison](https://github.com/nbn07/agent-tracedown#comparison) and [how Tracedown decides](HOW-IT-DECIDES.md).

## Does Tracedown need an LLM? What leaves my machine in judge mode?

No. The default rule-based analysis works without an LLM. If you enable the optional judge, a short transcript of the run and the rule findings are sent to the model provider you configure when the judge is needed.

See [how Tracedown decides](HOW-IT-DECIDES.md#5-the-judge-optional) and [configuration](CONFIGURATION.md).

## Why is the first guess often wrong, and what does the score mean?

Tracedown uses rules and evidence to find possible causes, but these do not always identify the correct step. The score is used to rank suspects, not to show the probability that a guess is correct. Always check the evidence before trusting a verdict.

See [how findings are ranked](HOW-IT-DECIDES.md#4-rank).

## Can I replay runs imported from Langfuse?

No. Imported runs can be diagnosed, but replay requires a run recorded with the Tracedown Python or TypeScript SDK because it needs access to your agent's code.

See [importing runs](importing.md#what-imported-runs-can-and-cannot-do).

## Why does pip say "no matching distribution"?

You may be using an older Python version. Tracedown requires Python 3.10 or newer. Check your Python version before installing.

See the [quick start guide](quickstart.md).

## Why do I get "attempt to write a readonly database"?

This can happen when the database is stored in a synced folder such as OneDrive, Dropbox, or iCloud. Sync clients may lock the file and prevent writes. Keep the database outside these folders.

See the [quick start guide](quickstart.md).

## Is there a paid version?

No. Tracedown is open source under Apache-2.0, and all features are available without a paid edition.

See the [license information](https://github.com/nbn07/agent-tracedown#license).