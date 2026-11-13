import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import * as tdn from "../src/index.js";
import { instrument, uninstrument } from "../src/integrations/anthropic.js";

const mem = new tdn.MemoryExporter();
beforeEach(() => {
  mem.clear();
  tdn.init({ project: "t", exporter: mem });
});

// A stand-in Anthropic client: only messages.create is faked, no network.
function fakeAnthropic() {
  const calls: any[] = [];
  return {
    calls,
    messages: {
      create: async (params: any) => {
        calls.push(params);
        return {
          id: "msg_1",
          type: "message",
          role: "assistant",
          model: params.model,
          content: [
            { type: "text", text: "Hi there" },
            { type: "tool_use", id: "toolu_1", name: "search", input: { q: "x" } },
          ],
          stop_reason: "tool_use",
          usage: { input_tokens: 9, output_tokens: 4 },
        };
      },
    },
  } as any;
}

test("messages calls are recorded as llm steps", async () => {
  const client = instrument(fakeAnthropic());
  const tid = await tdn.run("Bot", { input: "hi" }, async run => {
    const out = await client.messages.create({
      model: "claude-sonnet-4-5",
      system: "Be terse",
      messages: [{ role: "user", content: "Hello" }],
      max_tokens: 100,
    });
    // The wrapped call returns the recorded form, like the Python plugin.
    assert.equal(out.text, "Hi there");
    assert.deepEqual(out.tool_calls, [{ name: "search", arguments: { q: "x" } }]);
    assert.equal(out.finish_reason, "tool_use");
    assert.deepEqual(out.usage, { input_tokens: 9, output_tokens: 4 });
    return run.traceId;
  });
  const llm = mem.trace(tid).find(s => s.kind === "llm")!;
  assert.equal(llm.attributes["gen_ai.request.model"], "claude-sonnet-4-5");
  assert.equal(llm.attributes["gen_ai.provider.name"], "anthropic");
  assert.equal(llm.attributes["gen_ai.usage.input_tokens"], 9);
  assert.equal(llm.attributes["gen_ai.usage.output_tokens"], 4);
  // The recorded prompt keeps the system text beside the messages, as in Python.
  assert.deepEqual(JSON.parse(mem.blobs[llm.input_digest!]), {
    system: "Be terse",
    messages: [{ role: "user", content: "Hello" }],
  });
  // Untouched params (max_tokens) still reach the original method.
  assert.equal(client.calls[0].max_tokens, 100);
});

test("instrument is idempotent and uninstrument restores the raw client", async () => {
  const client = fakeAnthropic();
  const once = instrument(client).messages.create;
  assert.equal(instrument(client).messages.create, once);
  uninstrument(client);
  const tid = await tdn.run("Bot", { input: "hi" }, async run => {
    const raw = await client.messages.create({ model: "claude-sonnet-4-5", messages: [] });
    assert.ok(Array.isArray(raw.content));
    return run.traceId;
  });
  assert.equal(mem.trace(tid).filter(s => s.kind === "llm").length, 0);
});
