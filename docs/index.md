# Tracedown

**Find the step that caused your AI agent's failure.**

Your tracing tool shows what your agent did. Tracedown tells you which step broke it, shows the evidence, and replays the run with your fix. It is open source (Apache-2.0, every feature free) and works on top of the traces you already have.

![A support agent promises a $120 refund; the right answer is $80. Tracedown traces the claim to an outdated refund policy at step 2 and replays the run with the current one.](demo.gif)

**[Try the live demo](https://tracedown-demo.onrender.com/runs/)**: the eight demo failures, read-only, nothing to install. The free host sleeps when idle, so the first visit can take a minute.

## Start here

| | |
|---|---|
| [Quick start](quickstart.md) | One command: the server, the web app and eight demo failures |
| [Diagnose runs you already have](importing.md) | Paste a trace, or import from Langfuse or OpenTelemetry |
| [How Tracedown decides](HOW-IT-DECIDES.md) | The method, the evidence behind each verdict, and where it goes wrong |
| [Integrations](integrations.md) | One line for LangGraph, CrewAI, OpenAI Agents, the Vercel AI SDK and nine more |
| [Benchmarks](benchmarks.md) | Accuracy on public benchmarks, losses included |

## What you get for a failed run

- **The root cause:** the step where things went wrong, not just where they crashed.
- **The evidence:** what Tracedown saw, in words you can check.
- **Who was at fault:** the agent, the user, or nobody yet.
- **Each claim checked:** every sentence of the answer, with the source it came from, or none.
- **Replay with your fix:** re-run from the broken step; earlier steps come from the recording.

## Links

[GitHub](https://github.com/nbn07/agent-tracedown) · [PyPI](https://pypi.org/project/agent-tracedown-server/) · [npm](https://www.npmjs.com/package/agent-tracedown) · [Discussions](https://github.com/nbn07/agent-tracedown/discussions) · [Report a wrong diagnosis](https://github.com/nbn07/agent-tracedown/issues/new?template=wrong-diagnosis.yml)
