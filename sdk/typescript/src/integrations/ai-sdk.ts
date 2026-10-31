/**
 * Vercel AI SDK (`ai` v5, v6 and v7).
 *
 *     import { generateText } from "ai";
 *     import * as tdn from "agent-tracedown";
 *     import { traceModel, traceTools } from "agent-tracedown/ai-sdk";
 *
 *     const model = traceModel(openai("gpt-4o-mini"));
 *     await tdn.run("SupportBot", { input: question }, () =>
 *       generateText({ model, tools: traceTools(tools), prompt: question, stopWhen: stepCountIs(5) }));
 *
 * Every model call (generate or stream) becomes an LLM step with the prompt, the reply,
 * tool calls and token usage; every tool execution becomes a tool step. Steps outside
 * `tdn.run` are not traced. Replay works: recorded model replies and tool results are
 * served back, so `tdn.replay(bundle, { mode: "from-step", fromStep }, ...)` reruns an
 * AI SDK agent from the failing step without calling the model for earlier steps.
 */
import { _endSpan, _replayable, _startSpan, _withSpan, currentRun, toolCall, declareTools, type ToolDecl } from "../tracer.js";
import type { Span } from "../spans.js";

type AnyFn = (...args: any[]) => any;
interface ModelLike {
  provider: string;
  modelId: string;
  specificationVersion?: string;
  doGenerate: AnyFn;
  doStream: AnyFn;
  [k: string]: unknown;
}

/** Token counts from AI SDK usage objects (v4 nested `{total}` or v5 flat numbers). */
export function tokens(usage: any): { input?: number; output?: number; cacheRead?: number; reasoning?: number } {
  if (!usage || typeof usage !== "object") return {};
  const pick = (v: any): number | undefined => (typeof v === "number" ? v : typeof v?.total === "number" ? v.total : undefined);
  return {
    input: pick(usage.inputTokens),
    output: pick(usage.outputTokens),
    cacheRead: typeof usage.inputTokens?.cacheRead === "number" ? usage.inputTokens.cacheRead
      : typeof usage.cachedInputTokens === "number" ? usage.cachedInputTokens : undefined,
    reasoning: typeof usage.outputTokens?.reasoning === "number" ? usage.outputTokens.reasoning
      : typeof usage.reasoningTokens === "number" ? usage.reasoningTokens : undefined,
  };
}

function setUsage(span: Span, usage: unknown): void {
  const t = tokens(usage);
  span.update({
    "gen_ai.usage.input_tokens": t.input, "gen_ai.usage.output_tokens": t.output,
    "gen_ai.usage.cache_read.input_tokens": t.cacheRead, "gen_ai.usage.reasoning.output_tokens": t.reasoning,
  });
}

/** What we store as the step's output: the reply content in AI SDK form. */
interface Recorded { content: any[]; finishReason: unknown; usage: unknown }

function summarize(span: Span, rec: Recorded): void {
  const calls = rec.content.filter(c => c?.type === "tool-call");
  if (calls.length) span.set("tdn.llm.tool_calls", calls.map(c => c.toolName));
  const reason = (rec.finishReason as any)?.unified ?? rec.finishReason;
  if (typeof reason === "string") span.set("gen_ai.response.finish_reasons", [reason]);
  setUsage(span, rec.usage);
}

function declareFromParams(params: any): void {
  const run = currentRun();
  const root = run?.spans.find(s => s.parentSpanId === null);
  if (!root || root.attributes["tdn.tools.available"] || !Array.isArray(params?.tools)) return;
  declareTools(params.tools.filter((t: any) => t?.name).map((t: any): ToolDecl =>
    ({ name: t.name, description: t.description, schema: t.inputSchema ?? t.parameters })));
}

function stepAttrs(model: ModelLike, params: any): Record<string, unknown> {
  return {
    "gen_ai.request.model": model.modelId,
    "gen_ai.provider.name": String(model.provider ?? "").split(".")[0] || "custom",
    "gen_ai.request.temperature": params?.temperature,
    "gen_ai.request.max_tokens": params?.maxOutputTokens ?? params?.maxTokens,
    "gen_ai.request.seed": params?.seed,
  };
}

/** Rebuild a stream from a recorded reply, for replay of streamed calls. */
function replayStream(rec: Recorded): ReadableStream<any> {
  const parts: any[] = [{ type: "stream-start", warnings: [] }];
  rec.content.forEach((c, i) => {
    const id = String(i);
    if (c?.type === "text") parts.push({ type: "text-start", id }, { type: "text-delta", id, delta: c.text }, { type: "text-end", id });
    else if (c?.type === "reasoning") parts.push({ type: "reasoning-start", id }, { type: "reasoning-delta", id, delta: c.text }, { type: "reasoning-end", id });
    else parts.push(c);
  });
  parts.push({ type: "finish", finishReason: rec.finishReason, usage: rec.usage });
  return new ReadableStream({ start(ctrl) { for (const p of parts) ctrl.enqueue(p); ctrl.close(); } });
}

/**
 * A language-model middleware that records each model call as a Tracedown step. Use
 * with `wrapLanguageModel({ model, middleware: tracedownMiddleware() })`, or simply
 * `traceModel(model)`.
 */
export function tracedownMiddleware() {
  return {
    specificationVersion: "v3" as const,
    async wrapGenerate({ doGenerate, params, model }: { doGenerate: () => PromiseLike<any>; params: any; model: ModelLike }) {
      declareFromParams(params);
      return _withSpan(`chat ${model.modelId}`, "llm", stepAttrs(model, params), async span => {
        const run = currentRun();
        if (span && run) span.inputDigest = run.putBlob(params?.prompt ?? null);
        const served = _replayable(span, "llm");
        let result: any;
        if (served) {
          const rec = served.value as Recorded;
          result = { content: rec.content, finishReason: rec.finishReason, usage: rec.usage, warnings: [] };
        } else {
          result = await doGenerate();
        }
        if (span && run) {
          const rec: Recorded = { content: result.content ?? [], finishReason: result.finishReason, usage: result.usage };
          span.outputDigest = run.putBlob(rec);
          summarize(span, rec);
          if (result.response?.modelId) span.set("gen_ai.response.model", result.response.modelId);
        }
        return result;
      });
    },

    async wrapStream({ doStream, params, model }: { doStream: () => PromiseLike<any>; params: any; model: ModelLike }) {
      declareFromParams(params);
      const h = _startSpan(`chat ${model.modelId}`, "llm", stepAttrs(model, params));
      if (!h) return doStream();
      h.span.inputDigest = h.state.putBlob(params?.prompt ?? null);
      let served: { value: unknown } | null;
      try {
        served = _replayable(h.span, "llm");
      } catch (err) {
        _endSpan(h, err);
        throw err;
      }
      if (served) {
        const rec = served.value as Recorded;
        h.span.outputDigest = h.state.putBlob(rec);
        summarize(h.span, rec);
        _endSpan(h);
        return { stream: replayStream(rec) };
      }
      let result: any;
      try {
        result = await doStream();
      } catch (err) {
        _endSpan(h, err);
        throw err;
      }
      // Rebuild the reply from the stream parts as they pass through, untouched.
      const content: any[] = [];
      const open = new Map<string, any>();
      let finishReason: unknown, usage: unknown, failed: unknown;
      const tap = new TransformStream<any, any>({
        transform(part, ctrl) {
          switch (part?.type) {
            case "text-start": case "reasoning-start": {
              const c = { type: part.type === "text-start" ? "text" : "reasoning", text: "" };
              open.set(part.id, c);
              content.push(c);
              break;
            }
            case "text-delta": case "reasoning-delta": {
              const c = open.get(part.id);
              if (c) c.text += part.delta ?? part.textDelta ?? "";
              else content.push({ type: part.type === "text-delta" ? "text" : "reasoning", text: part.delta ?? part.textDelta ?? "" });
              break;
            }
            case "tool-call": case "tool-result": case "source": case "file":
              content.push(part);
              break;
            case "finish":
              finishReason = part.finishReason;
              usage = part.usage;
              break;
            case "error":
              failed = part.error;
              break;
          }
          ctrl.enqueue(part);
        },
        flush() {
          const rec: Recorded = { content, finishReason, usage };
          h.span.outputDigest = h.state.putBlob(rec);
          summarize(h.span, rec);
          _endSpan(h, failed);
        },
      });
      return { ...result, stream: result.stream.pipeThrough(tap) };
    },
  };
}

/** Wrap a model so every call is recorded (same as `wrapLanguageModel` with the middleware). */
export function traceModel<M>(input: M): M {
  const model = input as unknown as ModelLike & object;
  const mw = tracedownMiddleware();
  // A thin wrapper keeps provider, modelId and every other field of the model.
  return new Proxy(model, {
    get(target, prop, receiver) {
      if (prop === "doGenerate") {
        return (params: any) => mw.wrapGenerate({ doGenerate: () => target.doGenerate(params), params, model: target });
      }
      if (prop === "doStream") {
        return (params: any) => mw.wrapStream({ doStream: () => target.doStream(params), params, model: target });
      }
      return Reflect.get(target, prop, receiver);
    },
  }) as unknown as M;
}

/**
 * Record every tool execution as a tool step (arguments, result, errors). Tools without
 * `execute` (client-side tools) are left as they are.
 */
export function traceTools<T extends Record<string, any>>(tools: T): T {
  const out: Record<string, any> = {};
  for (const [name, tool] of Object.entries(tools)) {
    if (typeof tool?.execute !== "function") {
      out[name] = tool;
      continue;
    }
    const execute = tool.execute.bind(tool);
    out[name] = {
      ...tool,
      execute: (input: any, options: any) =>
        toolCall(name, (input ?? {}) as Record<string, unknown>, () => execute(input, options)),
    };
  }
  return out as T;
}
