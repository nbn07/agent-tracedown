/**
 * The first five demo agents from server/tracedown_server/demo/agents.py, in TypeScript. No API keys: the
 * "models" are deterministic stand-ins, so every run reproduces exactly.
 *
 *     npx tsx examples/demo.ts http://localhost:7070
 */
import * as tdn from "../src/index.js";

const CALENDAR_SCHEMA = {
  type: "object",
  required: ["title", "start"],
  properties: { title: { type: "string" }, start: { type: "string", format: "date-time" } },
};

// 1. wrong tool arguments: the model copies the weekday instead of resolving it to a date.
export const extractEventBuggy = () =>
  ({ text: JSON.stringify({ title: "Call with Priya", start: "Friday" }), usage: { input_tokens: 182, output_tokens: 19 } });
export const extractEventFixed = () =>
  ({ text: JSON.stringify({ title: "Call with Priya", start: "2026-11-06T15:00:00-05:00" }), usage: { input_tokens: 201, output_tokens: 27 } });
const createEvent = ({ title, start }: { title: string; start: string }) => ({ event_id: "evt_8812", title, start, status: "confirmed" });

export async function calendarAgent(message = "Book a call with Priya next Friday at 3pm", extractor: tdn.LlmFn = extractEventBuggy) {
  return tdn.run("CalendarAssistant", { input: message, attributes: { step_budget: 12 } }, async run => {
    const result = await tdn.agent("PlannerAgent", async () => {
      const tz = await tdn.memoryRead("user.timezone", "America/New_York");
      await tdn.decision(["create_event", "find_free_slot", "ask_clarifying_question"], "create_event", "user gave a day and a time");
      const raw = await tdn.llmCall<{ text: string }>(extractor, {
        model: "gpt-4o-mini", provider: "openai", temperature: 0.2, promptVersion: "extract-event@v3",
        prompt: [{ role: "system", content: `Extract the event. User timezone: ${tz}.` }, { role: "user", content: message }],
      });
      const event = JSON.parse(raw.text);
      return (await tdn.toolCall("calendar_create_event", event, createEvent, { schema: CALENDAR_SCHEMA }))!;
    }, { role: "planner" });
    const reply = await tdn.agent("WriterAgent", async () => {
      await tdn.handoff("PlannerAgent", "WriterAgent", result);
      return tdn.llmCall<{ text: string }>(() => ({ text: `Booked: ${result.title} on ${result.start}.` }),
        { model: "gpt-4o-mini", provider: "openai", prompt: result });
    }, { role: "writer" });
    run.output(reply.text);
    return run.traceId;
  });
}

// 2. outdated retrieval source
const POLICY_CHUNKS = [
  { id: "kb-refunds-2023#4", source: "refund-policy-2023.md", version: "2023.2", score: 0.91, stale: true,
    text: "Annual plan cancellations within 30 days receive a refund of $120." },
  { id: "kb-billing-faq#2", source: "billing-faq.md", version: "2026.1", score: 0.62,
    text: "Refunds are issued to the original payment method within 5 business days." },
];

export async function refundAgent(message = "I cancelled my annual plan after 10 days. How much will I get back?") {
  return tdn.run("SupportBot", { input: message }, async run => {
    const chunks = await tdn.agent("ResearchAgent", async () => {
      const c = await tdn.retrieval("annual plan refund amount", () => POLICY_CHUNKS, { source: "help-center" });
      await tdn.handoff("ResearchAgent", "WriterAgent", c.map(x => x.id));
      return c;
    }, { role: "research" });
    const answer = await tdn.agent("WriterAgent", () => tdn.llmCall<{ text: string }>(
      () => ({ text: "You will receive a refund of $120. Refunds are issued to the original payment method within 5 business days.",
               usage: { input_tokens: 312, output_tokens: 31 } }),
      { model: "claude-haiku-4-5", provider: "anthropic",
        prompt: { system: "Answer only from context.", context: chunks.map(c => c.text).join("\n"), question: message } }),
    { role: "writer" });
    run.output(answer.text);
    run.fail("customer was quoted an incorrect refund amount"); // found later: the current policy pays $80
    return run.traceId;
  });
}

// 3. loop
export async function researchLoopAgent(message = "What is the latest stable version of our internal SDK?") {
  return tdn.run("ResearchAssistant", { input: message, attributes: { step_budget: 6 } }, async run => {
    await tdn.agent("ResearchAgent", async () => {
      for (let i = 0; i < 4; i++) {
        await tdn.llmCall(() => ({ text: "I should search again." }), { model: "gpt-4o-mini", provider: "openai", prompt: message });
        await tdn.toolCall("web_search", { query: "internal SDK latest version" }, () => ({ results: [] }));
      }
    }, { role: "research" });
    run.output("I could not find the version.");
    run.fail("step budget exceeded without an answer");
    return run.traceId;
  });
}

// 4. unsupported claim
export async function pricingAgent(message = "How much is the Pro plan?") {
  return tdn.run("SalesAssistant", { input: message }, async run => {
    const answer = await tdn.agent("PricingAgent", async () => {
      const price = await tdn.toolCall("get_price", { plan: "pro" }, () => ({ plan: "pro", usd_per_month: 39 }));
      return tdn.llmCall<{ text: string }>(() => ({ text: "The Pro plan costs $49 per month and includes priority support." }),
        { model: "gpt-4o-mini", provider: "openai", prompt: { question: message, price } });
    });
    run.output(answer.text);
    return run.traceId;
  });
}

// 5. wrong tool: asked to cancel a subscription, the router picks the refund tool.
const ACCOUNT_TOOLS: tdn.ToolDecl[] = [
  { name: "refund_order", description: "Refund a single order that was charged by mistake",
    schema: { type: "object", required: ["order_id"], additionalProperties: false,
              properties: { order_id: { type: "string" }, amount: { type: "number" } } } },
  { name: "cancel_subscription", description: "Cancel a recurring subscription plan for the customer",
    schema: { type: "object", required: ["subscription_id"], additionalProperties: false,
              properties: { subscription_id: { type: "string" } } } },
];

export async function accountAgent(message = "Please cancel my subscription sub_981, I no longer need it.") {
  return tdn.run("AccountAgent", { input: message }, async run => {
    run.tools(ACCOUNT_TOOLS);
    await tdn.agent("RouterAgent", async () => {
      const pick = await tdn.llmCall<{ text: string }>(
        () => ({ text: JSON.stringify({ tool: "refund_order", args: { subscription_id: "sub_981" } }) }),
        { model: "gpt-4o-mini", provider: "openai", prompt: message });
      const choice = JSON.parse(pick.text);
      const schema = ACCOUNT_TOOLS.find(t => t.name === choice.tool)!.schema;
      await tdn.toolCall(choice.tool, choice.args, () => ({ ok: true }), { schema });
    });
    run.output("Done.");
    return run.traceId;
  });
}

// 6. drift: a prompt change. Prompt v4 shortens the system prompt and, as a side effect, the model
// starts calling the `escalate` tool and failing more.
function seeded(seed: string) { // small seeded generator (xmur3 hash feeding mulberry32): runs reproduce exactly
  let h = 1779033703 ^ seed.length;
  for (let i = 0; i < seed.length; i++) h = Math.imul(h ^ seed.charCodeAt(i), 3432918353), h = (h << 13) | (h >>> 19);
  let a = h >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), a | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export async function supportCohort(promptVersion: string, i: number) {
  const rnd = seeded(`${promptVersion}-${i}`);
  const question = ["Why was I charged twice?", "Can I change my billing date?", "Where is my invoice?"][Math.floor(rnd() * 3)];
  const v4 = promptVersion.endsWith("v4");
  return tdn.run("SupportBot", { input: question, attributes: { release: "2026.10." + (v4 ? "2" : "1") } }, async run => {
    await tdn.agent("BillingAgent", async () => {
      await tdn.llmCall(() => ({ text: "lookup", usage: { input_tokens: v4 ? 160 : 220, output_tokens: 20 } }),
        { model: "gpt-4o-mini", provider: "openai", prompt: question, promptVersion });
      await tdn.toolCall("get_invoices", { customer: `c${i}` }, () => ({ invoices: 2 }));
      if (v4 && rnd() < 0.6) await tdn.toolCall("escalate", { reason: "unsure" }, () => ({ ticket: "T-1" }));
      const failed = rnd() < (v4 ? 0.35 : 0.08);
      await tdn.llmCall(() => ({ text: "Here is what happened with your bill.", usage: { input_tokens: 300, output_tokens: 40 } }),
        { model: "gpt-4o-mini", provider: "openai", prompt: question, promptVersion });
      run.output("Here is what happened with your bill.");
      if (failed) run.fail("customer reopened the ticket");
    });
    return run.traceId;
  });
}

export const ALL = { calendar: calendarAgent, refund: refundAgent, loop: researchLoopAgent, pricing: pricingAgent, account: accountAgent };

if (import.meta.url === `file://${process.argv[1]}`) {
  const endpoint = process.argv[2] ?? "http://localhost:7070";
  tdn.init({ project: "demo-ts", endpoint });
  for (const [name, fn] of Object.entries(ALL)) {
    try {
      console.log(name, await fn());
    } catch (err) {
      console.log(name, "failed:", (err as Error).message);
    }
  }
  console.log("drift demo: 40 SupportBot runs on prompt billing@v3, then 40 on billing@v4 …");
  for (const v of ["billing@v3", "billing@v4"]) for (let i = 0; i < 40; i++) await supportCohort(v, i);
}
