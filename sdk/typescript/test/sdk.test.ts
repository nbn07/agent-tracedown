import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import * as tdn from "../src/index.js";
import { calendarAgent, extractEventFixed } from "../examples/demo.js";

const mem = new tdn.MemoryExporter();
beforeEach(() => {
  mem.clear();
  tdn.init({ project: "t", exporter: mem });
});

const root = (spans: tdn.SpanDict[]) => spans.find(s => s.parent_span_id === null)!;

test("a run records a tree of steps with OTel GenAI attributes", async () => {
  const tid = await tdn.run("Bot", { input: "hi" }, async run => {
    await tdn.agent("Planner", async () => {
      await tdn.llmCall(() => ({ text: "ok", usage: { input_tokens: 10, output_tokens: 2 } }),
        { model: "gpt-4o-mini", provider: "openai", prompt: "hi", contextLimit: 100 });
      await tdn.toolCall("lookup", { q: 1 }, ({ q }) => ({ q }));
    });
    run.output("done");
    return run.traceId;
  });
  const spans = mem.trace(tid);
  assert.equal(spans.length, 4);
  const r = root(spans);
  assert.equal(r.attributes["tdn.run.outcome"], "success");
  assert.equal(r.attributes["tdn.sdk.language"], "typescript");
  assert.equal(spans.at(-1)!.span_id, r.span_id, "root goes last");
  const llm = spans.find(s => s.kind === "llm")!;
  assert.equal(llm.attributes["gen_ai.operation.name"], "chat");
  assert.equal(llm.attributes["gen_ai.usage.input_tokens"], 10);
  assert.equal(llm.attributes["tdn.llm.context_utilization"], 0.1);
  assert.equal(llm.attributes["tdn.agent.name"], "Planner");
  assert.deepEqual(spans.map(s => s.attributes["tdn.step"]).sort(), [0, 1, 2, 3]);
  const planner = spans.find(s => s.kind === "agent" && s !== r)!;
  assert.equal(llm.parent_span_id, planner.span_id);
  for (const s of spans) assert.ok(s.end_time! >= s.start_time);
});

test("invalid tool arguments mark the step failed and throw", async () => {
  await assert.rejects(calendarAgent(), tdn.ToolError);
  const spans = mem.spans;
  const tool = spans.find(s => s.kind === "tool")!;
  assert.equal(tool.status.code, "ERROR");
  assert.equal(tool.attributes["tdn.tool.args_valid"], false);
  assert.match(String(tool.status.message), /start must be ISO-8601/);
  assert.equal(root(spans).attributes["tdn.run.outcome"], "failure");
});

test("calls outside a run are untraced but still work", async () => {
  assert.equal(await tdn.toolCall("t", { a: 1 }, ({ a }) => a + 1), 2);
  assert.equal(mem.spans.length, 0);
});

test("parallel runs keep their steps apart", async () => {
  const one = (n: string) => tdn.run(n, async run => {
    for (let i = 0; i < 3; i++) {
      await new Promise(r => setTimeout(r, Math.random() * 5));
      await tdn.toolCall("t", { i }, ({ i }) => i);
    }
    return run.traceId;
  });
  const ids = await Promise.all([one("A"), one("B"), one("C")]);
  for (const id of ids) {
    const spans = mem.trace(id);
    assert.equal(spans.length, 4);
    assert.deepEqual(spans.map(s => s.attributes["tdn.step"]).sort(), [0, 1, 2, 3]);
  }
});

test("identical payloads are stored once", async () => {
  await tdn.run("Bot", async () => {
    await tdn.toolCall("t", { a: 1 }, () => "same");
    await tdn.toolCall("t", { a: 1 }, () => "same");
  });
  assert.equal(Object.keys(mem.blobs).length, 2);
});

test("payload digests match the Python SDK byte for byte", () => {
  const value = { b: [1, 2.5, "x"], a: { "é": "naïve ☕ 😀", z: null, y: true }, "tab\t": "line\nbreak \"q\"" };
  const ts = tdn.canonicalJson(value);
  const py = execFileSync("python3", ["-c",
    "import json,sys,hashlib; v=json.loads(sys.stdin.read()); t=json.dumps(v, sort_keys=True, default=str); print(t)"],
    { input: JSON.stringify(value) }).toString().replace(/\n$/, "");
  assert.equal(ts, py);
});

test("redaction and capture off", async () => {
  tdn.init({ exporter: mem, redact: tdn.defaultRedactor });
  await tdn.run("Bot", { input: "mail me at a@b.com, key sk-abcdefghijklmnopqrstu" }, () => {});
  const text = Object.values(mem.blobs).join(" ");
  assert.ok(!text.includes("a@b.com") && text.includes("[EMAIL]") && text.includes("[API_KEY]"));
  mem.clear();
  tdn.init({ exporter: mem, captureContent: false });
  await tdn.run("Bot", { input: "secret" }, () => {});
  assert.equal(Object.keys(mem.blobs).length, 0);
  assert.ok(root(mem.spans).input_digest);
});

test("exact replay serves every step from the recording", async () => {
  let calls = 0;
  const agentFn = () => tdn.run("Bot", { input: "q" }, async run => {
    const a = await tdn.llmCall(() => { calls++; return { text: `answer ${calls}` }; }, { model: "m", prompt: "q" });
    const b = await tdn.toolCall("t", { x: 1 }, () => { calls++; return calls; });
    run.output([a, b]);
    return run.traceId;
  });
  const tid = await agentFn();
  const bundle = mem.bundle(tid);
  const replayed = await tdn.replay(bundle, { mode: "exact" }, agentFn);
  assert.equal(calls, 2, "nothing ran live");
  const r = root(mem.trace(replayed));
  assert.equal(r.attributes["tdn.replay.of"], tid);
  assert.deepEqual(r.attributes["tdn.replay.recorded_steps"], [1, 2]);
  assert.equal(r.output_digest, root(mem.trace(tid)).output_digest);
});

test("from-step replay with a fix turns the failed calendar run into a success", async () => {
  await assert.rejects(calendarAgent());
  const tid = root(mem.spans).trace_id;
  const bundle = mem.bundle(tid);
  const llmStep = bundle.spans.find(s => s.kind === "llm")!.attributes["tdn.step"] as number;
  const newId = await tdn.replay(bundle, { mode: "from-step", fromStep: llmStep },
    () => calendarAgent(undefined, extractEventFixed));
  const r = root(mem.trace(newId));
  assert.equal(r.attributes["tdn.run.outcome"], "success");
  assert.deepEqual(r.attributes["tdn.replay.recorded_steps"], [2]); // the memory read; step 1 is the agent span
});

test("exact replay detects changed agent code", async () => {
  const tid = await tdn.run("Bot", async run => {
    await tdn.toolCall("t", { x: 1 }, () => 1);
    return run.traceId;
  });
  await assert.rejects(tdn.replay(mem.bundle(tid), { mode: "exact" }, () => tdn.run("Bot", async () => {
    await tdn.toolCall("t", { x: 2 }, () => 1);
  })), tdn.ReplayDivergence);
});

test("swap replay runs LLM steps live with the new model", async () => {
  const agentFn = () => tdn.run("Bot", async run => {
    const a = await tdn.llmCall((_p, o) => ({ text: o.model }), { model: "old", prompt: "q" });
    run.output(a);
    return run.traceId;
  });
  const tid = await agentFn();
  const nid = await tdn.replay(mem.bundle(tid), { mode: "swap", overrides: { model: "new" } }, agentFn);
  const llm = mem.trace(nid).find(s => s.kind === "llm")!;
  assert.equal(llm.attributes["gen_ai.request.model"], "new");
  assert.equal(llm.attributes["tdn.replay.source"], "live");
});

test("stream mode exports steps as they finish, root last", async () => {
  const batches: number[] = [];
  tdn.init({ exporter: { export: spans => { batches.push(spans.length); mem.export(spans, {}); } }, stream: true });
  await tdn.run("Bot", async () => {
    await tdn.toolCall("a", {}, () => 1);
    await tdn.toolCall("b", {}, () => 2);
  });
  assert.deepEqual(batches, [1, 1, 1]);
  assert.equal(mem.spans.at(-1)!.parent_span_id, null);
});

test("ManualTrace builds a run from callbacks", async () => {
  const t = new tdn.ManualTrace("Crew", { input: "go" });
  t.start("a1", "invoke_agent Researcher", "agent");
  t.start("t1", "execute_tool search", "tool", { parent: "a1", input: { q: 1 }, attrs: { "gen_ai.tool.name": "search" } });
  t.start("r1", "retrieval", "retrieval", { parent: "a1" });
  t.end("r1", { output: [{ id: "c1", score: 0.4 }, { id: "c2", score: 0.9 }] });
  t.end("t1", { error: "timeout" });
  t.declareTools([{ name: "search", description: "web", side_effects: false }]);
  const tid = await t.finish({ error: "search failed" });
  const spans = mem.trace(tid);
  const tool = spans.find(s => s.kind === "tool")!;
  assert.equal(tool.attributes["tdn.agent.name"], "Researcher");
  assert.equal(tool.status.code, "ERROR");
  assert.equal(spans.find(s => s.kind === "retrieval")!.attributes["tdn.retrieval.max_score"], 0.9);
  assert.equal(spans.find(s => s.kind === "agent" && s.parent_span_id)!.attributes["tdn.unclosed"], true);
  const r = root(spans);
  assert.equal(r.attributes["tdn.run.outcome"], "failure");
  assert.deepEqual(r.attributes["tdn.tools.available"], [{ name: "search", description: "web", schema: null, side_effects: false }]);
});

test("OTLP export shape", () => {
  const body = tdn.toOtlpJson([{ trace_id: "a".repeat(32), span_id: "b".repeat(16), parent_span_id: null, name: "x", kind: "tool",
    start_time: 1, end_time: 2, status: { code: "ERROR", message: "boom" }, attributes: { n: 1, f: 0.5, ok: true, o: { a: 1 } },
    input_digest: "sha256:1", output_digest: null }]) as any;
  const span = body.resourceSpans[0].scopeSpans[0].spans[0];
  assert.equal(span.status.code, 2);
  const attrs = Object.fromEntries(span.attributes.map((a: any) => [a.key, a.value]));
  assert.deepEqual(attrs.n, { intValue: "1" });
  assert.deepEqual(attrs.f, { doubleValue: 0.5 });
  assert.deepEqual(attrs.o, { stringValue: '{"a":1}' });
  assert.deepEqual(attrs["tdn.input.digest"], { stringValue: "sha256:1" });
});

test("HttpExporter never throws when the server is down", async () => {
  const ex = new tdn.HttpExporter("http://127.0.0.1:9", { timeoutMs: 500 });
  tdn.init({ exporter: ex });
  await tdn.run("Bot", () => 1);
  assert.ok(ex.lastError);
});
