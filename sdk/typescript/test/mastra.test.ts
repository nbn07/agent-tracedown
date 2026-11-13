import { test, before } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { Agent } from "@mastra/core/agent";
import { createTool } from "@mastra/core/tools";
import { MockLanguageModelV3 } from "ai/test";
import { z } from "zod";
import * as tdn from "../src/index.js";
import { instrument } from "../src/integrations/mastra.js";

const REPO = resolve(import.meta.dirname, "../../..");
let hasServer = true;
try { execFileSync("tracedown-server", ["--help"], { stdio: "ignore" }); } catch { hasServer = false; }

const mem = new tdn.MemoryExporter();
before(() => tdn.init({ project: "t", exporter: mem }));

const usage = (i: number, o: number) => ({
  inputTokens: { total: i, noCache: i, cacheRead: 0, cacheWrite: 0 },
  outputTokens: { total: o, text: o, reasoning: 0 },
});
const toolCallReply = (name: string, args: object) => ({
  content: [{ type: "tool-call", toolCallId: "c1", toolName: name, input: JSON.stringify(args) }],
  finishReason: { unified: "tool-calls", raw: "tool_calls" }, usage: usage(10, 5), warnings: [],
});
const textReply = (text: string) => ({
  content: [{ type: "text", text }],
  finishReason: { unified: "stop", raw: "stop" }, usage: usage(12, 6), warnings: [],
});
const scriptedModel = (replies: any[]) => {
  let i = 0;
  return new MockLanguageModelV3({ doGenerate: async () => replies[Math.min(i++, replies.length - 1)] });
};

const weatherTool = (fail = false) => createTool({
  id: "get_weather", description: "Get the weather in a city",
  inputSchema: z.object({ city: z.string() }),
  execute: async ({ city }: { city: string }) => {
    if (fail) throw new Error("weather service unavailable (HTTP 503)");
    return { forecast: "sunny, 24C", city };
  },
});
const weatherAgent = (model: any, fail = false) => new Agent({
  id: "weather", name: "WeatherAgent", instructions: "Help with weather.", model, maxRetries: 0,
  tools: { get_weather: weatherTool(fail) },
} as any);

const ordered = (tid: string) => mem.trace(tid).sort((a, b) => (a.attributes["tdn.step"] as number) - (b.attributes["tdn.step"] as number));

test("a Mastra generate run becomes a Tracedown run with LLM and tool steps", async () => {
  const agent = weatherAgent(scriptedModel([toolCallReply("get_weather", { city: "Paris" }), textReply("It is sunny in Paris.")]));
  const recorder = instrument(agent);
  const result: any = await agent.generate("Weather in Paris?");
  assert.equal(result.text, "It is sunny in Paris.");

  const spans = ordered(recorder.traceIds[recorder.traceIds.length - 1]);
  assert.deepEqual(spans.map(s => s.kind), ["agent", "llm", "tool", "llm"]);
  const [root, llm1, toolSpan] = spans;
  assert.equal(root.attributes["gen_ai.agent.name"], "WeatherAgent");
  assert.equal(root.attributes["tdn.run.outcome"], "success");
  assert.equal(JSON.parse(mem.blobs[root.input_digest!]), "Weather in Paris?");
  assert.equal(JSON.parse(mem.blobs[root.output_digest!]), "It is sunny in Paris.");
  assert.deepEqual((root.attributes["tdn.tools.available"] as any[]).map(t => t.name), ["get_weather"]);
  assert.equal(llm1.name, "chat mock-model-id");
  assert.equal(llm1.attributes["gen_ai.usage.input_tokens"], 10);
  assert.equal(llm1.attributes["gen_ai.usage.output_tokens"], 5);
  assert.deepEqual(llm1.attributes["tdn.llm.tool_calls"], ["get_weather"]);
  assert.equal(toolSpan.attributes["gen_ai.tool.name"], "get_weather");
  assert.deepEqual(JSON.parse(mem.blobs[toolSpan.input_digest!]), { city: "Paris" });
  assert.deepEqual(JSON.parse(mem.blobs[toolSpan.output_digest!]), { forecast: "sunny, 24C", city: "Paris" });
});

test("a failing tool is recorded with its error", async () => {
  const agent = weatherAgent(scriptedModel([toolCallReply("get_weather", { city: "Paris" }), textReply("Sorry, no weather.")]), true);
  const recorder = instrument(agent);
  await agent.generate("Weather in Paris?");
  const spans = ordered(recorder.traceIds[recorder.traceIds.length - 1]);
  const toolSpan = spans.find(s => s.kind === "tool")!;
  assert.equal(toolSpan.status.code, "ERROR");
  assert.match(toolSpan.status.message ?? "", /weather service unavailable \(HTTP 503\)/);
  assert.deepEqual(JSON.parse(mem.blobs[toolSpan.input_digest!]), { city: "Paris" });
});

test("the failing run is diagnosed as a tool failure", { skip: !hasServer && "tracedown-server not installed" }, async () => {
  const agent = weatherAgent(scriptedModel([toolCallReply("get_weather", { city: "Paris" }), textReply("Sorry, no weather.")]), true);
  const recorder = instrument(agent);
  await agent.generate("Weather in Paris?");
  const tid = recorder.traceIds[recorder.traceIds.length - 1];
  const file = join(mkdtempSync(join(tmpdir(), "tdn-mastra-")), "bundle.json");
  writeFileSync(file, JSON.stringify(mem.bundle(tid)));
  const out = execFileSync("python3", ["-c", `
import json, sys
sys.path[:0] = ["${REPO}/sdk/python", "${REPO}/server"]
from tracedown_server.rca import analyze
data = json.load(open("${file}"))
rep = analyze(data["spans"], data["blobs"])
print(rep["root_cause"]["category"] if rep["root_cause"] else "none")
`], { encoding: "utf8" });
  assert.equal(out.trim(), "tool_failure");
});

test("a generate call that throws finishes the run as failed and rethrows", async () => {
  const model = new MockLanguageModelV3({ doGenerate: async () => { throw new Error("model endpoint down"); } });
  const agent = weatherAgent(model);
  const recorder = instrument(agent);
  await assert.rejects(() => agent.generate("Weather in Paris?"), /model endpoint down/);
  const spans = ordered(recorder.traceIds[recorder.traceIds.length - 1]);
  assert.equal(spans[0].attributes["tdn.run.outcome"], "failure");
});

test("instrumenting twice returns the same recorder", () => {
  const agent = weatherAgent(scriptedModel([textReply("hi")]));
  assert.equal(instrument(agent), instrument(agent));
});
