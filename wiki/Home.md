# Tracedown wiki

**Tracedown finds the step that caused your AI agent's failure.** Your tracing tool shows what the agent did. Tracedown tells you which step broke it, shows the evidence, suggests a fix and proves whether it works. It is open source (Apache-2.0), and every feature is free.

Think of a car mechanic. The warning light on the dashboard is the *symptom*. A good mechanic doesn't replace the light; they follow the wires back to the part that failed. Tracedown does the same with an agent run: it starts where the failure showed up and follows the data back to the step that caused it.

![Tracedown traces a wrong refund amount back to an outdated policy document](https://nbn07.github.io/agent-tracedown/demo.gif)

**[Try the live demo](https://tracedown-demo.onrender.com/runs/)**: eight demo failures, read-only, nothing to install. The free host sleeps when idle, so the first visit can take a minute.

## Two doors

| I want to… | Go to |
|---|---|
| **Use Tracedown** on my agents | The [documentation site](https://nbn07.github.io/agent-tracedown/): quick start, importing traces, integrations, configuration |
| **Understand it** in plain words | [How Tracedown Works](How-Tracedown-Works) and the [Glossary](Glossary) |
| **Help build it** | [Your First Contribution](Your-First-Contribution), then the [Architecture Tour](Architecture-Tour) |

The documentation site is the user manual. This wiki is for understanding the project and working on it: how the code fits together, how to add to it, why it was built this way, and what comes next.

## Pages

**Understand**
- [How Tracedown Works](How-Tracedown-Works): the method, in pictures
- [Glossary](Glossary): run, span, step, symptom, root cause, replay…
- [Accuracy and Benchmarks](Accuracy-and-Benchmarks): how often it is right, and how we measure it
- [Design Decisions](Design-Decisions): why rules first, why no percentages, why a stdlib-only SDK

**Contribute**
- [Your First Contribution](Your-First-Contribution): from picking an issue to a merged pull request
- [Development Setup](Development-Setup): Codespaces or your own machine
- [Architecture Tour](Architecture-Tour): where everything lives in the code
- [Writing a Detector](Writing-a-Detector): teach Tracedown a new kind of failure
- [Adding a Framework Integration](Adding-a-Framework-Integration): support a new agent framework
- [Adding a Demo Story](Adding-a-Demo-Story): a small agent that fails in a realistic way
- [Reporting a Wrong Diagnosis](Reporting-a-Wrong-Diagnosis): the most useful report you can send

**Run and maintain**
- [Troubleshooting](Troubleshooting): common errors and their fixes
- [Release Process](Release-Process): how a version reaches PyPI and npm
- [Roadmap](Roadmap): what is done, in progress and next
- [Community and Support](Community-and-Support): where to ask, and how to say hello

## At a glance

| | |
|---|---|
| Install | `pip install agent-tracedown-server && tracedown up` |
| Packages | [`agent-tracedown`](https://pypi.org/project/agent-tracedown/) (Python SDK), [`agent-tracedown-server`](https://pypi.org/project/agent-tracedown-server/), [`agent-tracedown` on npm](https://www.npmjs.com/package/agent-tracedown) |
| Works with | LangGraph, CrewAI, OpenAI Agents, Claude Agent SDK, Google ADK, Pydantic AI, AutoGen, Semantic Kernel, LlamaIndex, MCP, Vercel AI SDK; imports from Langfuse and OpenTelemetry |
| License | Apache-2.0, no paid edition |
| Source | [https://github.com/nbn07/agent-tracedown](https://github.com/nbn07/agent-tracedown) |
