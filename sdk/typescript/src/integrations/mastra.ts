/**
 * Mastra (`@mastra/core` agents).
 *
 *     import { instrument } from "agent-tracedown/mastra";
 *     const recorder = instrument(agent);   // then call agent.generate(...) as usual
 *
 * Mastra has no global tracing hook, so the agent's own methods are wrapped:
 * `generate` (and `stream`, best-effort) each become one Tracedown run. Each model
 * step in the result becomes an LLM span (model, token usage, the step's text and
 * the tools it called) and each tool call a tool span (arguments in, result or
 * error out). When `generate` throws, the run is finished with the error and the
 * exception re-raised.
 *
 * Mastra does not expose an agent's configured tool list on the instance, so the
 * run declares the tools the run actually called. For `stream`, the run is
 * recorded once the stream's text settles; the stream object itself is returned
 * untouched.
 */
import { ManualTrace } from "../tracer.js";

/** A safe plain copy of framework objects (class instances, getters) for payload capture. */
function plain(v: unknown): unknown {
  if (v === undefined) return null;
  try { return JSON.parse(JSON.stringify(v)); } catch { return String(v); }
}

function firstUserText(input: unknown): string | null {
  if (typeof input === "string") return input;
  if (!Array.isArray(input)) return null;
  for (const m of input) {
    if (!m || typeof m !== "object" || (m as any).role !== "user") continue;
    const content = (m as any).content;
    if (typeof content === "string") return content;
    if (Array.isArray(content)) {
      const t = content.map((c: any) => (typeof c?.text === "string" ? c.text : "")).join("");
      if (t) return t;
    }
    if (Array.isArray((m as any).parts)) {
      const t = (m as any).parts.map((c: any) => (typeof c?.text === "string" ? c.text : "")).join("");
      if (t) return t;
    }
  }
  return null;
}

type ToolCallInfo = { id: string; name: string; args: unknown };
type ToolOutcome = { result?: unknown; error?: string };

/** Tool calls a step made: the payload-wrapped `toolCalls`, or the step's content parts. */
function stepCalls(step: any): ToolCallInfo[] {
  const out: ToolCallInfo[] = [];
  for (const part of step?.toolCalls ?? []) {
    const p = part?.payload ?? part;
    if (p?.toolName) out.push({ id: String(p.toolCallId ?? `${out.length}`), name: p.toolName, args: p.args ?? p.input });
  }
  if (out.length) return out;
  for (const part of step?.content ?? []) {
    if (part?.type === "tool-call" && part.toolName) {
      out.push({ id: String(part.toolCallId ?? `${out.length}`), name: part.toolName, args: part.input ?? part.args });
    }
  }
  return out;
}

/** Tool outcomes keyed by call id, from the payload-wrapped results and the content parts —
 *  a failed call shows up only in the content parts, as an `error-text`/`error-json` output. */
function stepOutcomes(step: any): Map<string, ToolOutcome> {
  const out = new Map<string, ToolOutcome>();
  for (const part of step?.toolResults ?? []) {
    const p = part?.payload ?? part;
    if (!p?.toolCallId) continue;
    if (p.isError || p.error) out.set(String(p.toolCallId), { error: errorText(p.error ?? p.result) });
    else out.set(String(p.toolCallId), { result: p.result });
  }
  for (const part of step?.content ?? []) {
    if (part?.type !== "tool-result" || !part.toolCallId) continue;
    const id = String(part.toolCallId);
    if (out.has(id)) continue;
    const o = part.output ?? part.result;
    if (o && typeof o === "object" && (o.type === "error-text" || o.type === "error-json")) {
      out.set(id, { error: typeof o.value === "string" ? o.value : errorText(o.value) });
    } else {
      out.set(id, { result: o && typeof o === "object" && "value" in o ? o.value : o });
    }
  }
  return out;
}

function errorText(e: unknown): string {
  if (e instanceof Error) return `${e.name}: ${e.message}`;
  if (typeof e === "string") return e;
  try { return JSON.stringify(e); } catch { return String(e); }
}

const usageAttrs = (usage: any) => ({
  "gen_ai.usage.input_tokens": typeof usage?.inputTokens === "number" ? usage.inputTokens : undefined,
  "gen_ai.usage.output_tokens": typeof usage?.outputTokens === "number" ? usage.outputTokens : undefined,
});

export class MastraRecorder {
  readonly traceIds: string[] = [];
  readonly agent: any;
  readonly name: string;

  constructor(agent: any, name?: string) {
    this.agent = agent;
    this.name = name || agent?.name || agent?.id || "MastraAgent";
  }

  /** Record one finished generate/stream result as a run. */
  record(messages: unknown, result: any, modelHint?: string): ManualTrace {
    const model = result?.response?.modelId || modelHint || "model";
    const t = new ManualTrace(this.name, { input: firstUserText(messages) ?? plain(messages) });
    this.traceIds.push(t.traceId);
    const steps: any[] = Array.isArray(result?.steps) && result.steps.length ? result.steps : [{
      toolCalls: result?.toolCalls, toolResults: result?.toolResults, content: result?.content,
      text: result?.text, usage: result?.usage,
    }];
    const called = new Set<string>();
    steps.forEach((step, i) => {
      const calls = stepCalls(step);
      calls.forEach(c => called.add(c.name));
      const outcomes = stepOutcomes(step);
      const llmKey = `step:${i}:llm`;
      t.start(llmKey, `chat ${model}`, "llm", { input: i === 0 ? plain(messages) : undefined, attrs: {
        "gen_ai.request.model": model,
        "tdn.llm.tool_calls": calls.map(c => c.name),
      } });
      t.end(llmKey, { output: step?.text ? { text: step.text } : undefined, attrs: usageAttrs(step?.usage ?? result?.usage) });
      for (const call of calls) {
        const key = `step:${i}:tool:${call.id}`;
        t.start(key, `execute_tool ${call.name}`, "tool", { input: plain(call.args), attrs: { "gen_ai.tool.name": call.name } });
        const outcome = outcomes.get(call.id);
        t.end(key, { output: outcome && !outcome.error ? plain(outcome.result) : undefined, error: outcome?.error ?? null });
      }
    });
    if (called.size) t.declareTools([...called].map(name => ({ name })));
    return t;
  }
}

/** Wrap a Mastra agent's `generate` (and `stream`) so its runs are recorded. */
export function instrument(agent: any, opts: { name?: string } = {}): MastraRecorder {
  const existing = agent?._tdnRecorder as MastraRecorder | undefined;
  if (existing) return existing;
  const recorder = new MastraRecorder(agent, opts.name);
  const originalGenerate = agent.generate.bind(agent);
  agent.generate = async (messages: unknown, ...rest: unknown[]) => {
    let result: any;
    try {
      result = await originalGenerate(messages, ...rest);
    } catch (e) {
      const t = new ManualTrace(recorder.name, { input: firstUserText(messages) ?? plain(messages) });
      recorder.traceIds.push(t.traceId);
      await t.finish({ error: errorText(e) });
      throw e;
    }
    const t = recorder.record(messages, result);
    await t.finish({ output: typeof result?.text === "string" ? result.text : plain(result?.text ?? result?.object) });
    return result;
  };
  if (typeof agent.stream === "function") {
    const originalStream = agent.stream.bind(agent);
    agent.stream = async (messages: unknown, ...rest: unknown[]) => {
      const out: any = await originalStream(messages, ...rest);
      // best-effort: record once the stream's text settles; the caller keeps the stream.
      Promise.allSettled([out?.text, out?.usage, out?.toolCalls, out?.toolResults, out?.response])
        .then(async ([text, usage, toolCalls, toolResults, response]) => {
          if (text.status === "rejected") {
            const t = new ManualTrace(recorder.name, { input: firstUserText(messages) ?? plain(messages) });
            recorder.traceIds.push(t.traceId);
            await t.finish({ error: errorText(text.reason) });
            return;
          }
          const result = {
            text: text.value, usage: usage.status === "fulfilled" ? usage.value : undefined,
            toolCalls: toolCalls.status === "fulfilled" ? toolCalls.value : undefined,
            toolResults: toolResults.status === "fulfilled" ? toolResults.value : undefined,
            response: response.status === "fulfilled" ? response.value : undefined,
          };
          const t = recorder.record(messages, result);
          await t.finish({ output: result.text });
        })
        .catch(() => undefined);
      return out;
    };
  }
  agent._tdnRecorder = recorder;
  return recorder;
}
