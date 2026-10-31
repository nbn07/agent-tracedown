import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
// Runs against the installed `ai` (v7) by default; TDN_AI_PKG=ai-v5 or ai-v6 tests the older majors.
const AI = process.env.TDN_AI_PKG ?? "ai";
const ai: any = await import(AI);
const aiTest: any = await import(`${AI}/test`);
const { generateText, streamText, tool, stepCountIs, wrapLanguageModel } = ai;
const MockLanguageModel = aiTest.MockLanguageModelV4 ?? aiTest.MockLanguageModelV3 ?? aiTest.MockLanguageModelV2;
const V2 = !aiTest.MockLanguageModelV4 && !aiTest.MockLanguageModelV3;
import { z } from "zod";
import * as tdn from "../src/index.js";
import { traceModel, traceTools, tracedownMiddleware } from "../src/integrations/ai-sdk.js";

const mem = new tdn.MemoryExporter();
beforeEach(() => { mem.clear(); tdn.init({ project: "t", exporter: mem }); });

const usage = (i: number, o: number) => V2 ? { inputTokens: i, outputTokens: o, totalTokens: i + o } : {
  inputTokens: { total: i, noCache: i, cacheRead: 0, cacheWrite: 0 }, outputTokens: { total: o, text: o, reasoning: 0 },
};
const finish = (r: string) => (V2 ? r : { unified: r, raw: r });

// The calendar failure again, as an AI SDK agent: the model passes "Friday" as the time.
function calendarModel(start = "Friday") {
  let calls = 0;
  const model = new MockLanguageModel({
    provider: "openai.chat", modelId: "gpt-4o-mini",
    doGenerate: async () => (++calls === 1
      ? { content: [{ type: "tool-call", toolCallId: "c1", toolName: "create_event", input: JSON.stringify({ title: "Call with Priya", start }) }],
          finishReason: finish("tool-calls"), usage: usage(182, 19), warnings: [] }
      : { content: [{ type: "text", text: start === "Friday" ? "Sorry, I could not book it." : "Booked." }],
          finishReason: finish("stop"), usage: usage(220, 9), warnings: [] }) as any,
  });
  return { model, calls: () => calls };
}

let executed = 0;
const tools = {
  create_event: tool({
    description: "Create a calendar event",
    inputSchema: z.object({ title: z.string(), start: z.string() }),
    execute: async ({ title, start }: { title: string; start: string }) => {
      executed++;
      if (!/^\d{4}-\d{2}-\d{2}T/.test(start)) throw new Error(`calendar API: invalid start time '${start}'`);
      return { event_id: "evt_1", title, start };
    },
  }),
};

const agent = (model: any) => tdn.run("CalendarAssistant", { input: "Book a call with Priya next Friday at 3pm" }, async run => {
  const r = await generateText({ model: traceModel(model), tools: traceTools(tools), prompt: "Book a call with Priya next Friday at 3pm",
                                 stopWhen: stepCountIs(4) });
  run.output(r.text);
  if (r.text.startsWith("Sorry")) run.fail("the event was not booked");
  return run.traceId;
});

test(`[${AI}] generateText with tools records model and tool steps`, async () => {
  executed = 0;
  const tid = await agent(calendarModel().model);
  const spans = mem.trace(tid).sort((a, b) => (a.attributes["tdn.step"] as number) - (b.attributes["tdn.step"] as number));
  assert.deepEqual(spans.map(s => s.kind), ["agent", "llm", "tool", "llm"]);
  const [, llm1, toolSpan] = spans;
  assert.equal(llm1.attributes["gen_ai.request.model"], "gpt-4o-mini");
  assert.equal(llm1.attributes["gen_ai.provider.name"], "openai");
  assert.equal(llm1.attributes["gen_ai.usage.input_tokens"], 182);
  assert.deepEqual(llm1.attributes["tdn.llm.tool_calls"], ["create_event"]);
  assert.equal(toolSpan.attributes["gen_ai.tool.name"], "create_event");
  assert.equal(toolSpan.status.code, "ERROR");
  assert.match(toolSpan.status.message, /invalid start time 'Friday'/);
  assert.equal(JSON.parse(mem.blobs[toolSpan.input_digest!]).start, "Friday");
  const root = spans[0];
  assert.equal(root.attributes["tdn.run.outcome"], "failure");
  assert.equal((root.attributes["tdn.tools.available"] as any[])[0].name, "create_event");
});

test("replay from the failing model step with a fixed model, earlier steps served from the recording", async () => {
  const tid = await agent(calendarModel().model);
  const bundle = mem.bundle(tid);
  // step 1 is the first model call; replay the whole run but with a fixed model from step 1
  const fixed = calendarModel("2026-11-06T15:00:00-05:00");
  const nid = await tdn.replay(bundle, { mode: "from-step", fromStep: 1 }, () => agent(fixed.model));
  const root = mem.trace(nid).find(s => !s.parent_span_id)!;
  assert.equal(root.attributes["tdn.run.outcome"], "success");
  assert.equal(root.attributes["tdn.replay.of"], tid);
  assert.equal(fixed.calls(), 2);
});

test("exact replay of an AI SDK run calls neither the model nor the tools", async () => {
  executed = 0;
  const tid = await agent(calendarModel().model);
  assert.equal(executed, 1);
  const again = calendarModel();
  const nid = await tdn.replay(mem.bundle(tid), { mode: "exact" }, () => agent(again.model));
  assert.equal(again.calls(), 0);
  assert.equal(executed, 1);
  const root = mem.trace(nid).find(s => !s.parent_span_id)!;
  assert.deepEqual(root.attributes["tdn.replay.recorded_steps"], [1, 2, 3]);
  assert.equal(root.output_digest, mem.trace(tid).find(s => !s.parent_span_id)!.output_digest);
});

function streamingModel() {
  return new MockLanguageModel({
    provider: "anthropic.messages", modelId: "claude-haiku-4-5",
    doStream: async () => ({
      stream: new ReadableStream({
        start(c) {
          for (const p of [
            { type: "stream-start", warnings: [] },
            { type: "text-start", id: "t" }, { type: "text-delta", id: "t", delta: "Hello " },
            { type: "text-delta", id: "t", delta: "there" }, { type: "text-end", id: "t" },
            { type: "finish", finishReason: finish("stop"), usage: usage(12, 3) },
          ]) c.enqueue(p);
          c.close();
        },
      }),
    }) as any,
  });
}

test("streamText is recorded when the stream finishes, and replays from the recording", async () => {
  const go = (model: any) => tdn.run("Greeter", async run => {
    const r = streamText({ model: wrapLanguageModel({ model, middleware: tracedownMiddleware() }), prompt: "hi" });
    let text = "";
    for await (const d of r.textStream) text += d;
    run.output(text);
    return run.traceId;
  });
  const tid = await go(streamingModel());
  const llm = mem.trace(tid).find(s => s.kind === "llm")!;
  assert.equal(llm.attributes["gen_ai.usage.output_tokens"], 3);
  assert.deepEqual(JSON.parse(mem.blobs[llm.output_digest!]).content, [{ type: "text", text: "Hello there" }]);
  assert.ok(llm.end_time);

  const unused = new MockLanguageModel({ doStream: async () => { throw new Error("must not be called"); } });
  const nid = await tdn.replay(mem.bundle(tid), { mode: "exact" }, () => go(unused));
  const root = mem.trace(nid).find(s => !s.parent_span_id)!;
  assert.equal(JSON.parse(mem.blobs[root.output_digest!]), "Hello there");
});

test("model calls outside a run are not traced", async () => {
  const r = await generateText({ model: traceModel(calendarModel().model), prompt: "x" });
  assert.equal(r.toolCalls.length, 1);
  assert.equal(mem.spans.length, 0);
});
