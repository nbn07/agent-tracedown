/**
 * End to end against a real Tracedown server: the TypeScript demo agents must get the
 * same diagnosis (root cause, step, fault verdict) as the Python demo agents.
 * Skipped when `tracedown-server` is not installed.
 */
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { spawn, execFileSync, type ChildProcess } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import * as tdn from "../src/index.js";
import { ALL, supportCohort } from "../examples/demo.js";

const REPO = resolve(import.meta.dirname, "../../..");
let hasServer = true;
try { execFileSync("tracedown-server", ["--help"], { stdio: "ignore" }); } catch { hasServer = false; }

const port = 17000 + Math.floor(Math.random() * 2000);
const api = `http://127.0.0.1:${port}`;
let server: ChildProcess | undefined;

const get = async (path: string) => (await fetch(api + path)).json() as Promise<any>;

before(async () => {
  if (!hasServer) return;
  const db = join(mkdtempSync(join(tmpdir(), "tdn-ts-")), "t.db");
  server = spawn("tracedown-server", ["--host", "127.0.0.1", "--port", String(port), "--db", db], { stdio: "ignore" });
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(api + "/healthz")).ok) return; } catch { /* starting */ }
    await new Promise(r => setTimeout(r, 200));
  }
  throw new Error("server did not start");
});
after(() => { server?.kill(); });

async function waitAnalyzed(project: string, n: number) {
  for (let i = 0; i < 100; i++) {
    const runs = (await get(`/api/v1/runs?project=${project}&limit=100`)).runs;
    if (runs.length >= n && runs.every((r: any) => r.outcome !== "failure" || r.root_cause)) return runs;
    await new Promise(r => setTimeout(r, 200));
  }
  throw new Error(`runs for ${project} not analyzed`);
}

const summary = (runs: any[]) => Object.fromEntries(runs.map(r => [r.agent_name, {
  outcome: r.outcome, cause: r.root_cause?.label ?? null, step: r.root_cause?.step ?? null, fault: r.fault?.verdict ?? null,
}]));

test("TypeScript runs get the same diagnosis as Python runs", { skip: !hasServer && "tracedown-server not installed" }, async () => {
  tdn.init({ project: "ts", endpoint: api });
  for (const fn of Object.values(ALL)) await fn().catch(() => undefined);
  const ts = summary(await waitAnalyzed("ts", 5));

  execFileSync("python3", ["-c", `
import sys; sys.path[:0] = ["${REPO}/sdk/python", "${REPO}/server"]
import agent_tracedown as tdn
from tracedown_server.demo import agents as d
tdn.init(project="py", endpoint="${api}")
for name in ${JSON.stringify(Object.keys(ALL))}:  # the stories both SDKs have
    try: d.ALL[name]()
    except Exception: pass
`]);
  const py = summary(await waitAnalyzed("py", 5));

  assert.deepEqual(ts, py);
  assert.equal(ts.CalendarAssistant.cause, py.CalendarAssistant.cause);
  assert.ok(ts.CalendarAssistant.cause, "the calendar failure has a root cause");
  const detail = await get(`/api/v1/runs/${(await get("/api/v1/runs?project=ts&agent=CalendarAssistant")).runs[0].trace_id}`);
  assert.ok(JSON.stringify(detail).includes("ISO-8601"), "the run detail shows the validation error");
});

test("a TypeScript run replays from the server bundle with a fix", { skip: !hasServer && "tracedown-server not installed" }, async () => {
  tdn.init({ project: "ts-replay", endpoint: api });
  await ALL.calendar().catch(() => undefined);
  const [failed] = await waitAnalyzed("ts-replay", 1);
  const bundle = await tdn.fetchBundle(api, failed.trace_id);
  const { extractEventFixed, calendarAgent } = await import("../examples/demo.js");
  const fixed = await tdn.replay(bundle, { mode: "from-step", fromStep: failed.root_cause.step },
    () => calendarAgent(undefined, extractEventFixed));
  const runs = await waitAnalyzed("ts-replay", 2);
  const replayRun = runs.find((r: any) => r.trace_id === fixed);
  assert.equal(replayRun.outcome, "success");
  assert.equal(replayRun.replay_of, failed.trace_id);
});

test("AI SDK and OpenAI Agents runs get a root cause on the real server", { skip: !hasServer && "tracedown-server not installed" }, async () => {
  const { generateText, tool, stepCountIs }: any = await import("ai");
  const { MockLanguageModelV4 } = await import("ai/test");
  const { z } = await import("zod");
  const { traceModel, traceTools } = await import("../src/integrations/ai-sdk.js");
  tdn.init({ project: "frameworks", endpoint: api });

  // AI SDK: the model passes "Friday" as the start time and the calendar API rejects it.
  let n = 0;
  const model = new MockLanguageModelV4({ provider: "openai.chat", modelId: "gpt-4o-mini", doGenerate: async () => (++n === 1
    ? { content: [{ type: "tool-call", toolCallId: "c1", toolName: "create_event", input: '{"title":"Call with Priya","start":"Friday"}' }],
        finishReason: { unified: "tool-calls", raw: "" }, usage: { inputTokens: { total: 182 }, outputTokens: { total: 19 } }, warnings: [] }
    : { content: [{ type: "text", text: "Sorry, I could not book it." }], finishReason: { unified: "stop", raw: "" },
        usage: { inputTokens: { total: 220 }, outputTokens: { total: 9 } }, warnings: [] }) as any });
  const tools = { create_event: tool({ description: "Create a calendar event", inputSchema: z.object({ title: z.string(), start: z.string() }),
    execute: async ({ start }: { start: string }) => { throw new Error(`calendar API: invalid start time '${start}'`); } }) };
  await tdn.run("AiSdkCalendar", { input: "Book a call with Priya next Friday at 3pm" }, async run => {
    const r = await generateText({ model: traceModel(model), tools: traceTools(tools), prompt: "Book a call", stopWhen: stepCountIs(3) });
    run.output(r.text);
    run.fail("the event was not booked");
  });

  // OpenAI Agents: the order lookup tool fails.
  process.env.OPENAI_AGENTS_DISABLE_TRACING = "0";
  const oa: any = await import("@openai/agents");
  const { OpenAIResponsesModel } = await import("@openai/agents-openai");
  const { TracedownProcessor } = await import("../src/integrations/openai-agents.js");
  const proc = new TracedownProcessor();
  oa.setTraceProcessors([proc]);
  oa.setTracingDisabled(false);
  let k = 0;
  const client: any = { baseURL: "https://api.openai.com/v1", responses: { create: async () => ({ id: `r${++k}`, object: "response",
    model: "gpt-4o-mini", status: "completed", usage: { input_tokens: 90, output_tokens: 8, total_tokens: 98 },
    output: k === 1 ? [{ type: "function_call", id: "f", call_id: "c", name: "lookup_order", arguments: '{"order_id":"A-17"}', status: "completed" }]
      : [{ type: "message", id: "m", role: "assistant", status: "completed", content: [{ type: "output_text", text: "Sorry, I could not find it.", annotations: [] }] }] }) } };
  const lookup = oa.tool({ name: "lookup_order", description: "Look up an order", parameters: z.object({ order_id: z.string() }),
    execute: async () => { throw new Error("orders service: 503 unavailable"); } });
  await oa.run(new oa.Agent({ name: "SupportAgent", instructions: "Help.", tools: [lookup], model: new OpenAIResponsesModel(client, "gpt-4o-mini") }),
    "Where is my order A-17?");
  await proc.forceFlush();
  // the Agents SDK decides success itself; mark it failed as a user report would
  await fetch(`${api}/api/v1/runs/${proc.traceIds[0]}/feedback`, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ score: 0, comment: "order status was not given" }) });

  const runs = await waitAnalyzed("frameworks", 2);
  const show = runs.map((r: any) => ({ agent: r.agent_name, outcome: r.outcome, cause: r.root_cause?.label, step: r.root_cause?.step,
                                      fault: r.fault?.verdict, cost: r.cost_usd }));
  console.log(JSON.stringify(show, null, 1));
  const ai = runs.find((r: any) => r.agent_name === "AiSdkCalendar");
  assert.equal(ai.outcome, "failure");
  assert.ok(ai.root_cause, "AI SDK run has a root cause");
  assert.ok([1, 2].includes(ai.root_cause.step), "it points at the model call or the failed tool");
});

test("the TypeScript drift cohort produces a drift report", { skip: !hasServer && "tracedown-server not installed" }, async () => {
  tdn.init({ project: "ts-drift", endpoint: api });
  for (const v of ["billing@v3", "billing@v4"]) for (let i = 0; i < 40; i++) await supportCohort(v, i);
  await waitAnalyzed("ts-drift", 80);
  const { reports } = await get("/api/v1/drift/reports?agent=SupportBot");
  // the report must be about this project's prompt change, not just any SupportBot report
  const prompt = reports.find((r: any) => r.project === "ts-drift" && r.dimension === "prompt_version");
  assert.ok(prompt, `a prompt-version drift report for ts-drift (got ${JSON.stringify(reports.map((r: any) => [r.project, r.dimension]))})`);
  assert.equal(prompt.baseline, "billing@v3");
  assert.equal(prompt.candidate, "billing@v4");
  assert.equal(prompt.verdict, "drift");
});
