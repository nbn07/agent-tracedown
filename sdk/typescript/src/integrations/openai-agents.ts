/**
 * OpenAI Agents SDK for TypeScript (`@openai/agents`).
 *
 *     import { instrument } from "agent-tracedown/openai-agents";
 *     instrument();                     // alongside OpenAI's own tracing
 *     instrument({ replace: true });    // or make Tracedown the only processor
 *
 * Each SDK trace becomes one Tracedown run. Agent spans become agents (their tools and
 * handoffs declared), response/generation spans become LLM calls, function spans become
 * tool calls, handoff spans become handoffs and guardrails become decisions.
 */
import { ManualTrace } from "../tracer.js";

type AnySpan = {
  traceId: string;
  spanId: string;
  parentId: string | null;
  spanData: Record<string, any> & { type: string };
  error: { message?: string; data?: unknown } | null;
};
type AnyTrace = { traceId: string; name?: string; groupId?: string | null; metadata?: Record<string, unknown> };

/** The SDK's name for a trace when the caller gives none. */
const DEFAULT_NAME = "Agent workflow";

const maybeJson = (v: unknown): unknown => {
  if (typeof v === "string" && /^[[{]/.test(v)) {
    try { return JSON.parse(v); } catch { /* keep text */ }
  }
  return v;
};

/** A safe plain copy of SDK objects (class instances, getters) for payload capture. */
function plain(v: unknown): unknown {
  if (v === undefined) return null;
  try { return JSON.parse(JSON.stringify(v)); } catch { return String(v); }
}

/** The text a Responses API response said, plus the tools it asked for. */
export function responseText(resp: any): string | null {
  if (!resp) return null;
  if (typeof resp.output_text === "string" && resp.output_text) return resp.output_text;
  const parts: string[] = [];
  for (const item of resp.output ?? []) {
    if (item?.type === "function_call") parts.push(`[calls ${item.name}(${item.arguments ?? ""})]`);
    for (const c of item?.content ?? []) if (typeof c?.text === "string") parts.push(c.text);
  }
  return parts.join("\n") || null;
}

/** Chat-completions style output (generation spans from other providers). */
function generationText(output: unknown): string | null {
  if (!Array.isArray(output)) return typeof output === "string" ? output : null;
  const parts: string[] = [];
  for (const m of output) {
    if (!m || typeof m !== "object") { parts.push(String(m)); continue; }
    let content: any = (m as any).content;
    if (Array.isArray(content)) content = content.map((c: any) => c?.text ?? "").join("");
    if (content) parts.push(content);
    for (const call of (m as any).tool_calls ?? []) {
      if (call?.function?.name) parts.push(`[calls ${call.function.name}(${call.function.arguments ?? ""})]`);
    }
  }
  return parts.length ? parts.join("\n") : null;
}

function firstUserText(input: unknown): string | null {
  if (typeof input === "string") return input;
  if (!Array.isArray(input)) return null;
  for (const m of input) {
    if (m?.role !== "user") continue;
    if (typeof m.content === "string") return m.content;
    if (Array.isArray(m.content)) {
      const t = m.content.map((c: any) => c?.text ?? "").join("");
      if (t) return t;
    }
  }
  return null;
}

const errorOf = (span: AnySpan): string | null => {
  const e = span.error;
  if (!e) return null;
  return e.data !== undefined ? `${e.message}: ${typeof e.data === "string" ? e.data : JSON.stringify(e.data)}` : String(e.message ?? e);
};

export class TracedownProcessor {
  readonly traces = new Map<string, ManualTrace>();
  readonly traceIds: string[] = [];
  private lastOutput = new Map<string, unknown>();
  // parents of every SDK span, including kinds we don't record (task, turn), so a step
  // nests under its nearest recorded ancestor
  private parents = new Map<string, string | null>();
  private finishing: Promise<unknown>[] = [];

  start(): void {}

  async onTraceStart(trace: AnyTrace): Promise<void> {
    const t = new ManualTrace(trace.name || DEFAULT_NAME, {
      attributes: { "openai.trace_id": trace.traceId, ...(trace.groupId ? { "openai.group_id": trace.groupId } : {}) },
    });
    this.traces.set(trace.traceId, t);
    this.traceIds.push(t.traceId);
  }

  async onTraceEnd(trace: AnyTrace): Promise<void> {
    const t = this.traces.get(trace.traceId);
    if (!t) return;
    this.traces.delete(trace.traceId);
    for (const id of t.spans.keys()) this.parents.delete(id);
    const p = t.finish({ output: this.lastOutput.get(trace.traceId) });
    this.lastOutput.delete(trace.traceId);
    this.finishing.push(p);
    await p;
  }

  async onSpanStart(span: AnySpan): Promise<void> {
    const t = this.traces.get(span.traceId);
    if (!t) return;
    const d = span.spanData;
    this.parents.set(span.spanId, span.parentId);
    let parent = span.parentId;
    while (parent && !t.has(parent)) parent = this.parents.get(parent) ?? null;
    switch (d.type) {
      case "agent":
        // runs started without a workflow name are named after their first agent
        if (t.root.attributes["gen_ai.agent.name"] === DEFAULT_NAME && d.name) {
          t.root.name = `invoke_agent ${d.name}`;
          t.root.set("gen_ai.agent.name", d.name);
        }
        t.start(span.spanId, `invoke_agent ${d.name}`, "agent", { parent, attrs: {
          "gen_ai.agent.name": d.name, "tdn.agent.tools": d.tools ?? [], "tdn.agent.handoffs": d.handoffs ?? [] } });
        if (d.tools?.length && !t.root.attributes["tdn.tools.available"]) t.declareTools(d.tools.map((name: string) => ({ name })));
        break;
      case "response": case "generation":
        t.start(span.spanId, "chat", "llm", { parent });
        break;
      case "function":
        t.start(span.spanId, `execute_tool ${d.name}`, "tool", { parent, attrs: { "gen_ai.tool.name": d.name },
          input: d.input ? maybeJson(d.input) : undefined });
        break;
      case "handoff":
        t.start(span.spanId, `handoff ${d.from_agent} -> ${d.to_agent}`, "handoff", { parent,
          attrs: { "tdn.handoff.from": d.from_agent, "tdn.handoff.to": d.to_agent } });
        break;
      case "guardrail":
        t.start(span.spanId, `decision guardrail ${d.name}`, "decision", { parent, attrs: { "tdn.decision.options": ["pass", "tripwire"] } });
        break;
    }
  }

  async onSpanEnd(span: AnySpan): Promise<void> {
    const t = this.traces.get(span.traceId);
    if (!t || !t.has(span.spanId)) return;
    const d = span.spanData;
    const error = errorOf(span);
    const s = t.get(span.spanId)!;
    switch (d.type) {
      case "response": {
        const resp = d._response;
        const usage = resp?.usage;
        const text = responseText(resp);
        if (resp?.model) s.name = `chat ${resp.model}`;
        this.input(t, span.spanId, d._input);
        t.end(span.spanId, { output: { text }, error, attrs: {
          "gen_ai.request.model": resp?.model, "gen_ai.provider.name": "openai", "gen_ai.response.id": resp?.id ?? d.response_id,
          "gen_ai.usage.input_tokens": usage?.input_tokens, "gen_ai.usage.output_tokens": usage?.output_tokens,
          "gen_ai.usage.cache_read.input_tokens": usage?.input_tokens_details?.cached_tokens,
          "tdn.llm.tool_calls": (resp?.output ?? []).filter((o: any) => o?.type === "function_call").map((o: any) => o.name),
        } });
        if (text && !text.startsWith("[calls ")) this.lastOutput.set(span.traceId, text);
        break;
      }
      case "generation": {
        const text = generationText(plain(d.output));
        s.name = `chat ${d.model || "model"}`;
        this.input(t, span.spanId, d.input);
        t.end(span.spanId, { output: { text }, error, attrs: {
          "gen_ai.request.model": d.model, "gen_ai.provider.name": "openai",
          "gen_ai.usage.input_tokens": d.usage?.input_tokens, "gen_ai.usage.output_tokens": d.usage?.output_tokens,
        } });
        if (text) this.lastOutput.set(span.traceId, text);
        break;
      }
      case "function":
        // the SDK fills in the arguments after the span starts
        if (d.input && s.inputDigest === null) s.inputDigest = t.state.putBlob(maybeJson(d.input));
        t.end(span.spanId, { output: maybeJson(plain(d.output)), error });
        break;
      case "handoff":
        // the target agent is only known once the handoff resolves
        s.name = `handoff ${d.from_agent} -> ${d.to_agent}`;
        t.end(span.spanId, { error, attrs: { "tdn.handoff.from": d.from_agent, "tdn.handoff.to": d.to_agent } });
        break;
      case "guardrail": {
        const chosen = d.triggered ? "tripwire" : "pass";
        t.end(span.spanId, { output: chosen, error, attrs: { "tdn.decision.chosen": chosen, "tdn.guardrail.triggered": !!d.triggered } });
        break;
      }
      case "agent":
        // the SDK fills in tools and handoffs after the agent span starts
        if (d.tools?.length && !t.root.attributes["tdn.tools.available"]) t.declareTools(d.tools.map((name: string) => ({ name })));
        t.end(span.spanId, { error, attrs: { "tdn.agent.tools": d.tools, "tdn.agent.handoffs": d.handoffs } });
        break;
      default:
        t.end(span.spanId, { error });
    }
  }

  private input(t: ManualTrace, key: string, value: unknown): void {
    if (value === undefined || value === null) return;
    const v = plain(value);
    const s = t.get(key)!;
    s.inputDigest = t.state.putBlob(v);
    // the SDK trace has no input of its own: use the customer's first message
    if (t.root.inputDigest === null) {
      const first = firstUserText(v);
      if (first) t.root.inputDigest = t.state.putBlob(first);
    }
  }

  async shutdown(): Promise<void> {
    for (const [id, t] of [...this.traces]) {
      this.traces.delete(id);
      this.finishing.push(t.finish());
    }
    await this.forceFlush();
  }

  async forceFlush(): Promise<void> {
    await Promise.all(this.finishing.splice(0));
  }
}

/** Register the Tracedown processor with the OpenAI Agents SDK. */
export async function instrument(opts: { replace?: boolean } = {}): Promise<TracedownProcessor> {
  const sdk: any = await import("@openai/agents");
  const proc = new TracedownProcessor();
  if (opts.replace) sdk.setTraceProcessors([proc]);
  else sdk.addTraceProcessor(proc);
  return proc;
}
