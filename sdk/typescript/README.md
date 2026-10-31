# agent-tracedown (TypeScript)

The TypeScript SDK for [Tracedown](https://github.com/nbn07/agent-tracedown), the flight recorder for AI agents. It records each agent run step by step, so Tracedown can find the step that caused a failure, say who was at fault, and replay the run with your fix.

It works the same way as the Python SDK and writes the same data. A run recorded in TypeScript gets the same diagnosis as the same run recorded in Python: the test suite checks this against a real server.

```bash
npm install agent-tracedown
```

Node 18.17 or later. No dependencies.

## Record a run

```ts
import * as tdn from "agent-tracedown";

tdn.init({ project: "demo", endpoint: "http://localhost:7070" });

await tdn.run("CalendarAssistant", { input: message }, async run => {
  await tdn.agent("PlannerAgent", async () => {
    const raw = await tdn.llmCall(extractEvent, { model: "gpt-4o-mini", provider: "openai", prompt });
    await tdn.toolCall("calendar_create_event", JSON.parse(raw.text), createEvent, { schema: CALENDAR_SCHEMA });
  });
  run.output("Booked.");
});
```

| Call | Records |
|---|---|
| `tdn.run(name, { input }, fn)` | One end-to-end run. A thrown error or `run.fail(reason)` marks it failed |
| `tdn.agent(name, fn)` | A sub-agent; steps inside it belong to it |
| `tdn.llmCall(fn, { model, prompt, ... })` | A model call: prompt, reply, token usage, cost |
| `tdn.toolCall(name, args, fn, { schema })` | A tool call; `schema` checks the arguments first |
| `tdn.retrieval(query, fn)` | Retrieved chunks with source, score and version |
| `tdn.memoryRead`, `tdn.memoryWrite`, `tdn.decision`, `tdn.handoff` | Memory, choices and hand-offs between agents |

Calls made outside `tdn.run` are not recorded and never fail because of tracing. Runs started in parallel stay separate.

## Frameworks

### Vercel AI SDK (v5, v6 and v7)

```ts
import { generateText, stepCountIs } from "ai";
import { traceModel, traceTools } from "agent-tracedown/ai-sdk";

await tdn.run("SupportBot", { input: question }, () =>
  generateText({ model: traceModel(openai("gpt-4o-mini")), tools: traceTools(tools), prompt: question, stopWhen: stepCountIs(5) }));
```

Every model call, streamed or not, becomes a model step with the prompt, the reply, the tools it asked for and token usage. Every tool execution becomes a tool step. If you already use `wrapLanguageModel`, add `tracedownMiddleware()` to its middleware instead of calling `traceModel`.

### OpenAI Agents SDK

```ts
import { instrument } from "agent-tracedown/openai-agents";
await instrument();                    // alongside OpenAI's own tracing
// await instrument({ replace: true }) // or instead of it
```

Each SDK run becomes a Tracedown run with its agents, model calls, tool calls, hand-offs and guardrails.

### Anything else

Wrap the model and tool calls with `tdn.llmCall` and `tdn.toolCall`, or send OpenTelemetry GenAI spans to the server's `/v1/traces` endpoint.

## Replay a failure with your fix

```ts
const bundle = await tdn.fetchBundle("http://localhost:7070", traceId);
await tdn.replay(bundle, { mode: "from-step", fromStep: 4 }, () => calendarAgent(message, fixedExtractor));
```

Steps before step 4 are served from the recording, so earlier tools are not called again. From step 4 on, the run is live with your change, and the new run is linked to the old one. Modes: `exact` (everything from the recording), `from-step`, `swap` (model calls live with `overrides` such as a new model or prompt) and `full`.

Replay also works for Vercel AI SDK agents: recorded model replies and tool results are served back.

## Send runs elsewhere

```ts
tdn.init({ exporter: new tdn.FanoutExporter(
  new tdn.HttpExporter("http://localhost:7070", { project: "demo" }),
  new tdn.OtlpHttpExporter("http://localhost:4318/v1/traces"),   // Jaeger, Tempo, Datadog, a Collector
) });
```

Use `redact: tdn.defaultRedactor` to remove emails, API keys, card and phone numbers before anything leaves the process, or `captureContent: false` to send only the shape of each run.

## Development

```bash
npm install
npm test            # unit tests, framework tests, and end-to-end tests if tracedown-server is installed
npm run typecheck
```

`TDN_AI_PKG=ai-v5 node --import tsx --test test/ai-sdk.test.ts` runs the AI SDK tests against an older major version.

Apache-2.0.
