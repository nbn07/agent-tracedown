import { test, before } from "node:test";
import assert from "node:assert/strict";
import { z } from "zod";
import * as tdn from "../src/index.js";
import { TracedownProcessor } from "../src/integrations/openai-agents.js";

process.env.OPENAI_AGENTS_DISABLE_TRACING = "0";
const { Agent, run, tool, setTraceProcessors, setDefaultOpenAIClient, setTracingDisabled } = await import("@openai/agents");
const { OpenAIResponsesModel } = await import("@openai/agents-openai");

const mem = new tdn.MemoryExporter();
const proc = new TracedownProcessor();
before(() => {
  tdn.init({ project: "t", exporter: mem });
  setTraceProcessors([proc as any]);
  setTracingDisabled(false);
});

// A stand-in OpenAI client: the real Responses model code runs, only the HTTP call is faked.
function fakeClient(replies: any[]) {
  let i = 0;
  return {
    baseURL: "https://api.openai.com/v1",
    responses: {
      create: async () => {
        const out = replies[Math.min(i++, replies.length - 1)];
        return { id: `resp_${i}`, object: "response", model: "gpt-4o-mini", status: "completed", output: out,
                 usage: { input_tokens: 100 + i, output_tokens: 10, total_tokens: 110 + i,
                          input_tokens_details: { cached_tokens: 0 }, output_tokens_details: { reasoning_tokens: 0 } } };
      },
    },
  } as any;
}
const msg = (text: string) => ({ type: "message", id: "m", role: "assistant", status: "completed",
  content: [{ type: "output_text", text, annotations: [] }] });
const call = (name: string, args: object) => ({ type: "function_call", id: "fc", call_id: "call_1", name,
  arguments: JSON.stringify(args), status: "completed" });

test("an Agents SDK run becomes a Tracedown run with agent, LLM and tool steps", async () => {
  const lookup = tool({
    name: "lookup_order", description: "Look up an order by id",
    parameters: z.object({ order_id: z.string() }),
    execute: async ({ order_id }) => { throw new Error(`order ${order_id} not found`); },
  });
  const client = fakeClient([[call("lookup_order", { order_id: "A-17" })], [msg("Sorry, I could not find your order.")]]);
  const agent = new Agent({ name: "SupportAgent", instructions: "Help the customer.", tools: [lookup],
                            model: new OpenAIResponsesModel(client, "gpt-4o-mini") });
  const before = proc.traceIds.length;
  const result = await run(agent, "Where is my order A-17?");
  assert.equal(result.finalOutput, "Sorry, I could not find your order.");
  await proc.forceFlush();
  const tid = proc.traceIds[before];
  const spans = mem.trace(tid).sort((a, b) => (a.attributes["tdn.step"] as number) - (b.attributes["tdn.step"] as number));
  const kinds = spans.map(s => s.kind);
  assert.deepEqual(kinds.filter(k => k !== "agent"), ["llm", "tool", "llm"]);
  const agentSpan = spans.find(s => s.kind === "agent" && s.parent_span_id)!;
  assert.equal(agentSpan.attributes["gen_ai.agent.name"], "SupportAgent");
  const [llm1, toolSpan] = spans.filter(s => s.kind !== "agent");
  assert.equal(llm1.name, "chat gpt-4o-mini");
  assert.equal(llm1.attributes["gen_ai.usage.input_tokens"], 101);
  assert.deepEqual(llm1.attributes["tdn.llm.tool_calls"], ["lookup_order"]);
  assert.equal(toolSpan.attributes["tdn.agent.name"], "SupportAgent");
  assert.equal(toolSpan.status.code, "ERROR");
  assert.deepEqual(JSON.parse(mem.blobs[toolSpan.input_digest!]), { order_id: "A-17" });
  const root = spans[0];
  assert.equal(JSON.parse(mem.blobs[root.input_digest!]), "Where is my order A-17?");
  assert.equal(JSON.parse(mem.blobs[root.output_digest!]), "Sorry, I could not find your order.");
  assert.deepEqual((root.attributes["tdn.tools.available"] as any[]).map(t => t.name), ["lookup_order"]);
});

test("handoffs between agents are recorded", async () => {
  const client = fakeClient([[call("transfer_to_BillingAgent", {})], [msg("Your refund is on its way.")]]);
  const model = new OpenAIResponsesModel(client, "gpt-4o-mini");
  const billing = new Agent({ name: "BillingAgent", instructions: "Handle billing.", model });
  const triage = new Agent({ name: "TriageAgent", instructions: "Route the customer.", handoffs: [billing], model });
  const before = proc.traceIds.length;
  await run(triage, "I want a refund");
  await proc.forceFlush();
  const spans = mem.trace(proc.traceIds[before]);
  const h = spans.find(s => s.kind === "handoff")!;
  assert.equal(h.attributes["tdn.handoff.from"], "TriageAgent");
  assert.equal(h.attributes["tdn.handoff.to"], "BillingAgent");
  assert.equal(spans.find(s => !s.parent_span_id)!.attributes["gen_ai.agent.name"], "TriageAgent", "named after the first agent");
  const agents = spans.filter(s => s.kind === "agent" && s.parent_span_id).map(s => s.attributes["gen_ai.agent.name"]);
  assert.deepEqual(agents.sort(), ["BillingAgent", "TriageAgent"]);
});
