import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import * as tdn from "../src/index.js";
import { instrument, uninstrument } from "../src/integrations/openai.js";

const mem = new tdn.MemoryExporter();
beforeEach(() => {
  mem.clear();
  tdn.init({ project: "t", exporter: mem });
});

// A stand-in OpenAI client: only the create methods are faked, no network.
function fakeOpenAI() {
  const calls: { kind: string; params: any }[] = [];
  return {
    calls,
    chat: {
      completions: {
        create: async (params: any) => {
          calls.push({ kind: "chat", params });
          return {
            id: "chatcmpl_1",
            model: params.model,
            choices: [{
              index: 0,
              finish_reason: "stop",
              message: {
                role: "assistant",
                content: "Hello back",
                tool_calls: [{ id: "call_1", type: "function", function: { name: "lookup", arguments: "{\"q\":1}" } }],
              },
            }],
            usage: { prompt_tokens: 42, completion_tokens: 7, total_tokens: 49 },
          };
        },
      },
    },
    responses: {
      create: async (params: any) => {
        calls.push({ kind: "responses", params });
        return {
          id: "resp_1",
          model: params.model,
          status: "completed",
          output: [{
            type: "message", id: "m", role: "assistant", status: "completed",
            content: [{ type: "output_text", text: "Resp hello", annotations: [] }],
          }],
          output_text: "Resp hello",
          usage: { input_tokens: 11, output_tokens: 3, total_tokens: 14 },
        };
      },
    },
  } as any;
}

test("chat completions calls are recorded as llm steps", async () => {
  const client = instrument(fakeOpenAI());
  const tid = await tdn.run("Bot", { input: "hi" }, async run => {
    const out = await client.chat.completions.create({
      model: "gpt-4o-mini",
      messages: [{ role: "user", content: "Hello" }],
      temperature: 0.2,
    });
    // The wrapped call returns the recorded form, like the Python plugin.
    assert.equal(out.text, "Hello back");
    assert.deepEqual(out.tool_calls, [{ name: "lookup", arguments: "{\"q\":1}" }]);
    assert.equal(out.finish_reason, "stop");
    assert.deepEqual(out.usage, { input_tokens: 42, output_tokens: 7 });
    return run.traceId;
  });
  const llm = mem.trace(tid).find(s => s.kind === "llm")!;
  assert.equal(llm.attributes["gen_ai.request.model"], "gpt-4o-mini");
  assert.equal(llm.attributes["gen_ai.provider.name"], "openai");
  assert.equal(llm.attributes["gen_ai.usage.input_tokens"], 42);
  assert.equal(llm.attributes["gen_ai.usage.output_tokens"], 7);
  assert.deepEqual(JSON.parse(mem.blobs[llm.input_digest!]), [{ role: "user", content: "Hello" }]);
  assert.equal(JSON.parse(mem.blobs[llm.output_digest!]).text, "Hello back");
  // The original method saw the caller's params, temperature included.
  assert.equal(client.calls[0].kind, "chat");
  assert.equal(client.calls[0].params.temperature, 0.2);
});

test("responses calls are recorded as llm steps", async () => {
  const client = instrument(fakeOpenAI());
  const tid = await tdn.run("Bot", { input: "hi" }, async run => {
    const out = await client.responses.create({ model: "gpt-4o-mini", input: "Hello" });
    assert.equal(out.text, "Resp hello");
    assert.deepEqual(out.usage, { input_tokens: 11, output_tokens: 3 });
    return run.traceId;
  });
  const llm = mem.trace(tid).find(s => s.kind === "llm")!;
  assert.equal(llm.attributes["gen_ai.request.model"], "gpt-4o-mini");
  assert.equal(llm.attributes["gen_ai.usage.input_tokens"], 11);
  assert.equal(JSON.parse(mem.blobs[llm.input_digest!]), "Hello");
  assert.equal(client.calls[0].kind, "responses");
});

test("instrument is idempotent and uninstrument restores the raw client", async () => {
  const client = fakeOpenAI();
  const once = instrument(client).chat.completions.create;
  assert.equal(instrument(client).chat.completions.create, once);
  uninstrument(client);
  const tid = await tdn.run("Bot", { input: "hi" }, async run => {
    const raw = await client.chat.completions.create({ model: "gpt-4o-mini", messages: [] });
    // Raw SDK shape again: a choices array, not the recorded form.
    assert.ok(Array.isArray(raw.choices));
    return run.traceId;
  });
  assert.equal(mem.trace(tid).filter(s => s.kind === "llm").length, 0);
});
