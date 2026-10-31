/**
 * Run/step tracing with async-context propagation, plus replay interception.
 *
 *     import * as tdn from "agent-tracedown";
 *     tdn.init({ project: "demo", endpoint: "http://localhost:7070" });
 *     await tdn.run("CalendarAssistant", { input: msg }, async run => {
 *       await tdn.agent("PlannerAgent", async () => {
 *         const date = await tdn.llmCall(extract, { model: "gpt-4o-mini", prompt: p });
 *         await tdn.toolCall("calendar_create_event", { start: date }, createEvent);
 *       });
 *       run.output("Booked.");
 *     });
 *
 * The API mirrors the Python SDK, so runs from either language look the same on the
 * server, and the same failure rules, fault verdict and replay apply.
 */
import { AsyncLocalStorage } from "node:async_hooks";
import { createHash } from "node:crypto";
import { ConsoleExporter, HttpExporter, type Blobs, type Exporter } from "./exporters.js";
import { Span, canonicalJson, digestOf, newTraceId, type SpanDict, type SpanKind } from "./spans.js";
import { SDK_VERSION } from "./version.js";

export { SDK_VERSION };

// --------------------------------------------------------------------------- config

export interface Config {
  project: string;
  exporter: Exporter | null;
  captureContent: boolean;
  redact: ((value: unknown) => unknown) | null;
  stream: boolean;
}

export interface InitOptions {
  project?: string;
  /** Send runs to this Tracedown server. Ignored when `exporter` is given. */
  endpoint?: string;
  apiKey?: string;
  exporter?: Exporter;
  /** Record prompts, tool arguments and outputs (needed for replay). Default true. */
  captureContent?: boolean;
  redact?: (value: unknown) => unknown;
  /** Export each step as it finishes (live view) instead of the whole run at the end. */
  stream?: boolean;
}

let config: Config = { project: "default", exporter: new ConsoleExporter(), captureContent: true, redact: null, stream: false };

/** Configure the SDK. With no endpoint or exporter, runs print to stderr. */
export function init(opts: InitOptions = {}): Config {
  const project = opts.project ?? "default";
  const exporter = opts.exporter
    ?? (opts.endpoint ? new HttpExporter(opts.endpoint, { apiKey: opts.apiKey, project }) : new ConsoleExporter());
  config = { project, exporter, captureContent: opts.captureContent ?? true, redact: opts.redact ?? null, stream: !!opts.stream };
  return config;
}

export function getConfig(): Config {
  return config;
}

// --------------------------------------------------------------------------- replay state

/** Thrown when a replayed run stops matching the recording before the live point. */
export class ReplayDivergence extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ReplayDivergence";
  }
}

export class ToolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ToolError";
  }
}

export type ReplayMode = "exact" | "from-step" | "swap" | "full";

export interface ReplayOverrides {
  model?: string;
  temperature?: number;
  prompt?: unknown;
  llmFn?: LlmFn;
  /** Apply the overrides only to these LLM steps. */
  steps?: number[];
}

class Replay {
  served: number[] = [];
  live: number[] = [];
  firstDivergence: number | null = null;
  constructor(readonly sourceTraceId: string, readonly mode: ReplayMode, readonly fromStep: number | null,
              readonly overrides: ReplayOverrides, readonly steps: Map<number, SpanDict>, readonly blobs: Blobs) {}

  recordedFor(step: number, kind: SpanKind): SpanDict | null {
    if (this.mode === "full") return null;
    const rec = this.steps.get(step);
    const mustRecord = this.mode === "exact" || (this.mode === "from-step" && step < (this.fromStep ?? 0));
    if (!rec || rec.kind !== kind) {
      if (mustRecord) {
        // The agent did something the recording does not have at this step. Running it
        // live would execute real side effects in a replay that promised none.
        const found = rec ? `a ${rec.kind} step (${rec.name})` : "nothing";
        throw new ReplayDivergence(`step ${step}: the agent made a ${kind} call where the recording has ${found}`);
      }
      return null;
    }
    if (this.mode === "exact") return rec;
    if (this.mode === "from-step") return step < (this.fromStep ?? 0) ? rec : null;
    if (this.mode === "swap") return kind === "llm" ? null : rec;
    return null;
  }
}

// --------------------------------------------------------------------------- run state

export class RunState {
  spans: Span[] = [];
  blobs: Blobs = {};
  step = 0;
  private exported = new Set<string>();
  private exportedBlobs = new Set<string>();
  private pending: Promise<void>[] = [];

  constructor(readonly traceId: string, readonly replay: Replay | null = null) {}

  nextStep(): number {
    return this.step++;
  }

  /** Export spans not yet sent, with the blobs not yet sent. */
  flush(spans: Span[]): Promise<void> {
    const exporter = config.exporter;
    if (!exporter) return Promise.resolve();
    const todo = spans.filter(sp => !this.exported.has(sp.spanId));
    if (!todo.length) return Promise.resolve();
    const blobs: Blobs = {};
    for (const [d, c] of Object.entries(this.blobs)) if (!this.exportedBlobs.has(d)) blobs[d] = c;
    for (const sp of todo) this.exported.add(sp.spanId);
    for (const d of Object.keys(blobs)) this.exportedBlobs.add(d);
    const p = Promise.resolve()
      .then(() => exporter.export(todo.map(sp => sp.toDict()), blobs))
      .catch(err => { process.stderr.write(`[agent-tracedown] export failed: ${err}\n`); });
    this.pending.push(p);
    return p;
  }

  /** Wait for every export started so far (streamed steps included). */
  async drain(): Promise<void> {
    while (this.pending.length) await Promise.all(this.pending.splice(0));
  }

  /** Store a payload by content digest and return the digest. */
  putBlob(value: unknown): string {
    if (config.redact) value = config.redact(value);
    const text = canonicalJson(value);
    const d = digestOf(text);
    if (config.captureContent) this.blobs[d] = text;
    return d;
  }
}

interface Ctx {
  run?: RunState;
  span?: Span | null;
  pendingReplay?: Replay;
}

const als = new AsyncLocalStorage<Ctx>();
const ctx = (): Ctx => als.getStore() ?? {};

export function currentTraceId(): string | null {
  return ctx().run?.traceId ?? null;
}

function getBlob(blobs: Blobs, d: string | null | undefined): unknown {
  if (!d || !(d in blobs)) throw new ReplayDivergence(`recorded payload ${d} is missing; was content capture off?`);
  return JSON.parse(blobs[d]);
}

// --------------------------------------------------------------------------- span helpers

function nearestAgent(state: RunState, span: Span | null | undefined): string | null {
  const byId = new Map(state.spans.map(s => [s.spanId, s] as const));
  while (span) {
    if (span.kind === "agent") return String(span.attributes["gen_ai.agent.name"] ?? span.name);
    span = span.parentSpanId ? byId.get(span.parentSpanId) : undefined;
  }
  return null;
}

/**
 * Run `fn` inside a new step span. Outside `tdn.run` nothing is traced and `fn` gets
 * `null`: tracing never breaks the agent.
 */
async function withSpan<T>(name: string, kind: SpanKind, attrs: Record<string, unknown>,
                           fn: (span: Span | null) => T | Promise<T>): Promise<T> {
  const c = ctx();
  const state = c.run;
  if (!state) return fn(null);
  const parent = c.span ?? null;
  const span = new Span({ traceId: state.traceId, name, kind, parentSpanId: parent?.spanId ?? null });
  span.update(attrs);
  span.set("tdn.step", state.nextStep());
  const agentName = nearestAgent(state, parent);
  if (agentName && kind !== "agent") span.set("tdn.agent.name", agentName);
  state.spans.push(span);
  try {
    return await als.run({ ...c, span }, () => fn(span));
  } catch (err) {
    if (span.statusCode !== "ERROR") {
      span.error(errorText(err));
      // The bare message, so a replay can raise exactly what the agent saw the first time.
      if (err instanceof Error) span.set("tdn.error.message", err.message).set("tdn.error.type", err.name);
    }
    throw err;
  } finally {
    span.end();
    if (config.stream) void state.flush([span]);
  }
}

function errorText(err: unknown): string {
  if (err instanceof Error) return `${err.name}: ${err.message}`;
  return String(err);
}

export class RunHandle {
  constructor(private state: RunState, private root: Span) {}
  get traceId(): string {
    return this.state.traceId;
  }
  /** Record the final response of the run. */
  output(value: unknown): void {
    this.root.outputDigest = this.state.putBlob(value);
  }
  /** Mark the run failed without throwing (e.g. the user reported a wrong answer). */
  fail(reason: string): void {
    this.root.error(reason);
    this.root.set("tdn.run.outcome", "failure");
  }
  tag(tags: Record<string, unknown>): void {
    for (const [k, v] of Object.entries(tags)) this.root.set(`tdn.tag.${k}`, v);
  }
  /** User feedback, e.g. 1 = thumbs up, 0 = thumbs down. */
  feedback(score: number, comment?: string): void {
    this.root.set("tdn.feedback.score", score);
    if (comment) this.root.set("tdn.feedback.comment", comment);
  }
  /** Declare the tools offered to the model: [{name, description, schema}]. */
  tools(tools: ToolDecl[]): void {
    declareToolsOn(this.root, tools);
  }
}

export interface RunOptions {
  input?: unknown;
  /** Extra root attributes; keys without a dot get a `tdn.` prefix. */
  attributes?: Record<string, unknown>;
}

/** One end-to-end agent run = one trace. Resolves after the run has been exported. */
export async function run<T>(name: string, opts: RunOptions, fn: (run: RunHandle) => T | Promise<T>): Promise<T>;
export async function run<T>(name: string, fn: (run: RunHandle) => T | Promise<T>): Promise<T>;
export async function run<T>(name: string, a: RunOptions | ((run: RunHandle) => T | Promise<T>),
                             b?: (run: RunHandle) => T | Promise<T>): Promise<T> {
  const opts = typeof a === "function" ? {} : a;
  const fn = (typeof a === "function" ? a : b)!;
  const outer = ctx();
  const replay = outer.pendingReplay ?? null;
  const state = new RunState(newTraceId(), replay);
  const root = new Span({ traceId: state.traceId, name: `invoke_agent ${name}`, kind: "agent" });
  root.set("gen_ai.agent.name", name).set("tdn.run.root", true).set("tdn.step", state.nextStep());
  root.set("tdn.sdk.version", SDK_VERSION).set("tdn.sdk.language", "typescript").set("tdn.project", config.project);
  for (const [k, v] of Object.entries(opts.attributes ?? {})) root.set(k.includes(".") ? k : `tdn.${k}`, v);
  if (replay) root.set("tdn.replay.of", replay.sourceTraceId).set("tdn.replay.mode", replay.mode);
  if (opts.input !== undefined) root.inputDigest = state.putBlob(opts.input);
  state.spans.push(root);
  const handle = new RunHandle(state, root);
  try {
    const result = await als.run({ run: state, span: root }, () => fn(handle));
    if (!("tdn.run.outcome" in root.attributes)) {
      root.set("tdn.run.outcome", root.statusCode === "ERROR" ? "failure" : "success");
    }
    return result;
  } catch (err) {
    root.error(errorText(err));
    root.set("tdn.run.outcome", "failure");
    throw err;
  } finally {
    for (const sp of state.spans) {
      if (sp !== root && sp.endTime === null) { // e.g. a model stream nobody finished reading
        sp.set("tdn.unclosed", true);
        sp.end();
      }
    }
    root.end();
    if (replay) {
      root.set("tdn.replay.recorded_steps", [...replay.served]);
      root.set("tdn.replay.live_steps", [...replay.live]);
      root.set("tdn.replay.first_divergence", replay.firstDivergence);
      // The same replay may drive several runs in a test; reset its bookkeeping.
      replay.served = [];
      replay.live = [];
      replay.firstDivergence = null;
    }
    void state.flush([...state.spans.filter(s => s !== root), root]);
    await state.drain();
  }
}

/** A (sub-)agent inside the run, e.g. PlannerAgent, ResearchAgent. */
export function agent<T>(name: string, fn: () => T | Promise<T>,
                         opts: { role?: string; graphNode?: string } = {}): Promise<T> {
  return withSpan(`invoke_agent ${name}`, "agent",
    { "gen_ai.agent.name": name, "tdn.agent.role": opts.role, "tdn.agent.graph_node": opts.graphNode }, () => fn());
}

// --------------------------------------------------------------------------- step primitives

export interface ToolDecl {
  name: string;
  description?: string;
  schema?: JsonSchema;
  parameters?: JsonSchema;
  input_schema?: JsonSchema;
  side_effects?: boolean;
}

function declareToolsOn(root: Span, tools: ToolDecl[]): void {
  root.set("tdn.tools.available", tools.filter(t => t.name).map(t => ({
    name: t.name,
    description: (t.description ?? "").slice(0, 500),
    schema: t.schema ?? t.parameters ?? t.input_schema ?? null,
    ...(t.side_effects !== undefined ? { side_effects: !!t.side_effects } : {}),
  })));
}

/** Record the tools available to the model in this run (enables the wrong-tool check). */
export function declareTools(tools: ToolDecl[]): void {
  const state = ctx().run;
  const root = state?.spans.find(sp => sp.parentSpanId === null);
  if (root) declareToolsOn(root, tools);
}

/** If replay serves this step from the recording, return `{value}`; else null. */
function replayable(span: Span | null, kind: SpanKind): { value: unknown } | null {
  const state = ctx().run;
  if (!span || !state?.replay) return null;
  const rp = state.replay;
  const step = span.attributes["tdn.step"] as number;
  const rec = rp.recordedFor(step, kind);
  if (!rec) {
    rp.live.push(step);
    span.set("tdn.replay.source", "live");
    return null;
  }
  if (rec.input_digest && rec.input_digest !== span.inputDigest) {
    const hasOverrides = Object.keys(rp.overrides).length > 0;
    if (rp.mode === "exact" || (kind === "llm" && !hasOverrides)) {
      // Nothing was meant to change, so the agent code itself changed.
      throw new ReplayDivergence(`step ${step} (${span.name}) input differs from the recording; ` +
        "the agent changed before the replay point");
    }
    // A live step upstream changed this step's input: run it live and note the divergence.
    if (rp.firstDivergence === null) rp.firstDivergence = step;
    rp.live.push(step);
    span.set("tdn.replay.source", "live-diverged");
    return null;
  }
  rp.served.push(step);
  span.set("tdn.replay.source", "recorded");
  // A step that failed has no output; the caller replays its recorded error.
  if (!rec.output_digest && rec.status?.code === "ERROR") return { value: undefined };
  return { value: getBlob(rp.blobs, rec.output_digest) };
}

export interface LlmUsage { input_tokens?: number; output_tokens?: number }
export type LlmResult = string | { text?: string; usage?: LlmUsage; cost_usd?: number; [k: string]: unknown };
export type LlmFn = (prompt: any, opts: { model: string; temperature?: number; [k: string]: unknown }) => unknown;

export interface LlmCallOptions {
  prompt: unknown;
  model: string;
  provider?: string;
  temperature?: number;
  seed?: number;
  contextLimit?: number;
  promptVersion?: string;
  costUsd?: number;
  /** Passed through to `fn`. */
  params?: Record<string, unknown>;
}

/**
 * Call a model. `fn(prompt, {model, temperature, ...params})` returns text, or an
 * object with `text` and optional `usage` {input_tokens, output_tokens} and `cost_usd`.
 */
export async function llmCall<R = LlmResult>(fn: LlmFn, opts: LlmCallOptions): Promise<R> {
  const state = ctx().run;
  const rp = state?.replay;
  let { model, temperature, prompt } = opts;
  if (rp && state && Object.keys(rp.overrides).length) {
    const only = rp.overrides.steps;
    if (!only || only.includes(state.step)) {
      model = rp.overrides.model ?? model;
      temperature = rp.overrides.temperature ?? temperature;
      prompt = rp.overrides.prompt ?? prompt;
      fn = rp.overrides.llmFn ?? fn;
    }
  }
  return withSpan(`chat ${model}`, "llm", {
    "gen_ai.request.model": model, "gen_ai.provider.name": opts.provider ?? "custom",
    "gen_ai.request.temperature": temperature, "gen_ai.request.seed": opts.seed,
    "tdn.llm.context_limit": opts.contextLimit, "tdn.prompt.version": opts.promptVersion,
  }, async span => {
    if (span && state) span.inputDigest = state.putBlob(prompt);
    const served = replayable(span, "llm");
    const result = served ? served.value : await fn(prompt, { model, temperature, ...(opts.params ?? {}) });
    if (span && state) {
      span.outputDigest = state.putBlob(result);
      recordUsage(span, result, opts.contextLimit);
      const r = result as { cost_usd?: unknown } | null;
      const cost = opts.costUsd ?? (r && typeof r === "object" && typeof r.cost_usd === "number" ? r.cost_usd : undefined);
      if (cost !== undefined) span.set("tdn.llm.cost_usd", Number(cost));
    }
    return result as R;
  });
}

function recordUsage(span: Span, result: unknown, contextLimit?: number): void {
  const usage = result && typeof result === "object" ? (result as { usage?: LlmUsage }).usage : undefined;
  if (!usage || typeof usage !== "object") return;
  span.update({ "gen_ai.usage.input_tokens": usage.input_tokens, "gen_ai.usage.output_tokens": usage.output_tokens });
  if (contextLimit && usage.input_tokens) {
    span.set("tdn.llm.context_utilization", Math.round((usage.input_tokens / contextLimit) * 1e4) / 1e4);
  }
}

export interface JsonSchema {
  type?: string;
  properties?: Record<string, JsonSchema & { format?: string; pattern?: string }>;
  required?: string[];
  [k: string]: unknown;
}

/** Tiny JSON-schema subset: required, type, pattern, format=date-time. */
export function validateArgs(args: Record<string, unknown>, schema?: JsonSchema | null): string[] {
  if (!schema) return [];
  const errors: string[] = [];
  const props = schema.properties ?? {};
  for (const req of schema.required ?? []) if (!(req in args)) errors.push(`missing required argument '${req}'`);
  const typeOk: Record<string, (v: unknown) => boolean> = {
    string: v => typeof v === "string",
    integer: v => Number.isInteger(v),
    number: v => typeof v === "number",
    boolean: v => typeof v === "boolean",
    object: v => !!v && typeof v === "object" && !Array.isArray(v),
    array: v => Array.isArray(v),
  };
  for (const [k, v] of Object.entries(args)) {
    const spec = props[k];
    if (!spec) continue;
    const t = spec.type;
    if (t && typeOk[t] && !typeOk[t](v)) {
      errors.push(`${k} must be ${t}, got ${Array.isArray(v) ? "array" : v === null ? "null" : typeof v}`);
      continue;
    }
    if (typeof v === "string") {
      if (spec.format === "date-time" && !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(v)) {
        errors.push(`${k} must be ISO-8601 date-time, got '${v}'`);
      }
      if (spec.pattern && !new RegExp(spec.pattern).test(v)) errors.push(`${k} does not match '${spec.pattern}'`);
    }
  }
  return errors;
}

export interface ToolCallOptions {
  schema?: JsonSchema;
  /** Throw a ToolError on invalid arguments or a recorded tool error (default true). */
  raiseOnError?: boolean;
}

/** Execute a tool. Validates `args` against `schema` and records the result. */
export async function toolCall<A extends Record<string, unknown>, R>(
  name: string, args: A, fn: (args: A) => R | Promise<R>, opts: ToolCallOptions = {},
): Promise<R | undefined> {
  const state = ctx().run;
  const raise = opts.raiseOnError ?? true;
  return withSpan(`execute_tool ${name}`, "tool", { "gen_ai.tool.name": name }, async span => {
    const errors = validateArgs(args, opts.schema);
    if (span && state) {
      span.inputDigest = state.putBlob(args);
      span.set("tdn.tool.args_valid", errors.length === 0);
      if (opts.schema) {
        span.set("tdn.tool.schema_hash", "sha256:" +
          createHash("sha256").update(canonicalJson(opts.schema)).digest("hex").slice(0, 16));
      }
      if (errors.length) span.set("tdn.tool.validation_errors", errors);
    }
    const served = replayable(span, "tool");
    if (served && span && state?.replay) {
      const rec = state.replay.steps.get(span.attributes["tdn.step"] as number)!;
      if (rec.status?.code === "ERROR") {
        span.error(rec.status.message);
        const message = typeof rec.attributes["tdn.error.message"] === "string"
          ? rec.attributes["tdn.error.message"] as string : rec.status.message;
        span.set("tdn.error.message", message);
        if (raise) {
          // Same message and type name as the original, since agents often pass
          // `String(error)` on to the model.
          const e = new ToolError(message);
          if (typeof rec.attributes["tdn.error.type"] === "string") e.name = rec.attributes["tdn.error.type"] as string;
          span.set("tdn.error.type", e.name);
          throw e;
        }
        return undefined;
      }
      span.outputDigest = state.putBlob(served.value);
      return served.value as R;
    }
    if (errors.length) {
      const msg = errors.join("; ");
      span?.error(msg);
      if (raise) throw new ToolError(msg);
      return undefined;
    }
    const result = await fn(args);
    if (span && state) span.outputDigest = state.putBlob(result);
    return result;
  });
}

export interface Chunk { id?: string; text?: string; source?: string; score?: number; version?: string; stale?: boolean; [k: string]: unknown }

function chunkSummary(chunks: unknown[]): Record<string, unknown>[] {
  return chunks.filter((c): c is Chunk => !!c && typeof c === "object").map(c => {
    const out: Record<string, unknown> = {};
    for (const k of ["id", "source", "score", "version", "stale"]) if (k in c) out[k] = c[k];
    return out;
  });
}

function setRetrieval(span: Span, chunks: unknown[]): void {
  span.set("tdn.retrieval.count", chunks.length);
  span.set("tdn.retrieval.chunks", chunkSummary(chunks));
  const scores = chunks.map(c => (c as Chunk)?.score).filter((s): s is number => typeof s === "number");
  if (scores.length) span.set("tdn.retrieval.max_score", Math.max(...scores));
}

/**
 * Retrieve chunks. Each chunk: {id, text, source, score, version?, stale?}.
 * `kbSnapshot` names the index build, so drift reports can compare knowledge-base versions.
 */
export async function retrieval<C extends Chunk>(query: string, fn: (query: string) => C[] | Promise<C[]>,
                                                 opts: { source?: string; kbSnapshot?: string } = {}): Promise<C[]> {
  const state = ctx().run;
  return withSpan("retrieval", "retrieval", {
    "tdn.retrieval.query": query, "tdn.retrieval.source": opts.source, "tdn.kb.snapshot": opts.kbSnapshot,
  }, async span => {
    if (span && state) span.inputDigest = state.putBlob(query);
    const served = replayable(span, "retrieval");
    const chunks = (served ? served.value : await fn(query)) as C[];
    if (span && state) {
      span.outputDigest = state.putBlob(chunks);
      setRetrieval(span, chunks);
    }
    return chunks;
  });
}

export async function memoryWrite(key: string, value: unknown): Promise<void> {
  const state = ctx().run;
  await withSpan(`memory write ${key}`, "memory", { "tdn.memory.op": "write", "tdn.memory.key": key }, span => {
    if (span && state) {
      span.inputDigest = state.putBlob(value);
      span.set("tdn.memory.value_digest", span.inputDigest);
    }
  });
}

/** Record a read of `key` that returned `value` (the store itself is yours). */
export async function memoryRead<V>(key: string, value: V): Promise<V> {
  const state = ctx().run;
  return withSpan(`memory read ${key}`, "memory", { "tdn.memory.op": "read", "tdn.memory.key": key }, span => {
    const served = replayable(span, "memory");
    if (served) value = served.value as V;
    if (span && state) {
      span.outputDigest = state.putBlob(value);
      span.set("tdn.memory.value_digest", span.outputDigest);
    }
    return value;
  });
}

/** Record an explicit choice (a plan step, a routing decision). */
export async function decision(options: string[], chosen: string, rationale = "", preconditionOk = true): Promise<string> {
  const state = ctx().run;
  await withSpan(`decision ${chosen}`, "decision", {
    "tdn.decision.options": options, "tdn.decision.chosen": chosen,
    "tdn.decision.rationale": rationale, "tdn.decision.precondition_ok": preconditionOk,
  }, span => {
    if (span && state) span.outputDigest = state.putBlob(chosen);
  });
  return chosen;
}

export async function handoff(fromAgent: string, toAgent: string, payload?: unknown): Promise<void> {
  const state = ctx().run;
  await withSpan(`handoff ${fromAgent} -> ${toAgent}`, "handoff",
    { "tdn.handoff.from": fromAgent, "tdn.handoff.to": toAgent }, span => {
      if (span && state && payload !== undefined) span.inputDigest = state.putBlob(payload);
    });
}

// --------------------------------------------------------------------------- replay

export interface Bundle { spans: SpanDict[]; blobs?: Blobs }

/**
 * Replay a recorded run. Inside `fn`, call your agent as usual; the next `tdn.run` is
 * replayed. Modes: exact (all recorded), from-step (steps < N recorded), swap (LLM steps
 * live with `overrides` such as model, temperature, prompt or llmFn), full (all live).
 */
export async function replay<T>(bundle: Bundle, opts: { mode?: ReplayMode; fromStep?: number; overrides?: ReplayOverrides },
                                fn: () => T | Promise<T>): Promise<T> {
  const mode = opts.mode ?? "exact";
  if (!["exact", "from-step", "swap", "full"].includes(mode)) throw new Error(`unknown replay mode '${mode}'`);
  if (mode === "from-step" && opts.fromStep === undefined) throw new Error("from-step replay needs fromStep");
  const spans = bundle.spans;
  const roots = spans.filter(s => s.parent_span_id === null);
  const steps = new Map<number, SpanDict>();
  for (const s of spans) if (typeof s.attributes["tdn.step"] === "number") steps.set(s.attributes["tdn.step"] as number, s);
  const rp = new Replay((roots[0] ?? spans[0]).trace_id, mode, opts.fromStep ?? null, opts.overrides ?? {},
    steps, bundle.blobs ?? {});
  return als.run({ ...ctx(), pendingReplay: rp }, fn);
}

/** Download a run's spans and blobs from a Tracedown server for replay. */
export async function fetchBundle(endpoint: string, traceId: string, apiKey?: string): Promise<Bundle> {
  const resp = await fetch(`${endpoint.replace(/\/+$/, "")}/api/v1/runs/${traceId}/bundle`, {
    headers: apiKey ? { Authorization: `Bearer ${apiKey}` } : {},
    signal: AbortSignal.timeout(10_000),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status} fetching bundle ${traceId}`);
  return (await resp.json()) as Bundle;
}

// --------------------------------------------------------------------------- manual traces (framework adapters)

/**
 * A run built from callbacks instead of nested functions.
 *
 * Framework adapters receive start/end events keyed by the framework's own ids, so
 * async context does not apply. Keys map to spans; parents are given explicitly.
 *
 *     const t = new ManualTrace("ResearchCrew", { input });
 *     t.start("agent-1", "Researcher", "agent");
 *     t.start("tool-7", "execute_tool web_search", "tool", { parent: "agent-1", input: args,
 *       attrs: { "gen_ai.tool.name": "web_search" } });
 *     t.end("tool-7", { output: result });
 *     await t.finish({ output: answer });
 */
export class ManualTrace {
  readonly state: RunState;
  readonly root: Span;
  readonly spans = new Map<string, Span>();
  finished = false;

  constructor(name: string, opts: RunOptions = {}) {
    this.state = new RunState(newTraceId());
    this.root = new Span({ traceId: this.state.traceId, name: `invoke_agent ${name}`, kind: "agent" });
    this.root.set("gen_ai.agent.name", name).set("tdn.run.root", true).set("tdn.step", this.state.nextStep());
    this.root.set("tdn.sdk.version", SDK_VERSION).set("tdn.sdk.language", "typescript").set("tdn.project", config.project);
    for (const [k, v] of Object.entries(opts.attributes ?? {})) this.root.set(k.includes(".") ? k : `tdn.${k}`, v);
    if (opts.input !== undefined) this.root.inputDigest = this.state.putBlob(opts.input);
    this.state.spans.push(this.root);
  }

  get traceId(): string {
    return this.state.traceId;
  }

  has(key: string): boolean {
    return this.spans.has(key);
  }

  get(key: string): Span | undefined {
    return this.spans.get(key);
  }

  start(key: string, name: string, kind: SpanKind,
        opts: { parent?: string | null; input?: unknown; attrs?: Record<string, unknown> } = {}): Span {
    const parent = (opts.parent ? this.spans.get(opts.parent) : undefined) ?? this.root;
    const span = new Span({ traceId: this.traceId, name, kind, parentSpanId: parent.spanId });
    span.update(opts.attrs);
    span.set("tdn.step", this.state.nextStep());
    if (kind === "agent") {
      if (!("gen_ai.agent.name" in span.attributes)) span.set("gen_ai.agent.name", name.replace(/^invoke_agent /, "").trim());
    } else {
      const a = nearestAgent(this.state, parent);
      if (a) span.set("tdn.agent.name", a);
    }
    if (opts.input !== undefined) span.inputDigest = this.state.putBlob(opts.input);
    this.state.spans.push(span);
    this.spans.set(key, span);
    return span;
  }

  set(key: string, attrs: Record<string, unknown>): void {
    this.spans.get(key)?.update(attrs);
  }

  end(key: string, opts: { output?: unknown; error?: string | null; attrs?: Record<string, unknown> } = {}): Span | undefined {
    const span = this.spans.get(key);
    if (!span || span.endTime !== null) return span;
    span.update(opts.attrs);
    if (opts.output !== undefined) {
      span.outputDigest = this.state.putBlob(opts.output);
      if (span.kind === "retrieval" && Array.isArray(opts.output)) setRetrieval(span, opts.output);
    }
    if (opts.error) span.error(opts.error);
    span.end();
    if (config.stream) void this.state.flush([span]);
    return span;
  }

  declareTools(tools: ToolDecl[]): void {
    declareToolsOn(this.root, tools);
  }

  async finish(opts: { output?: unknown; error?: string | null } = {}): Promise<string> {
    if (this.finished) return this.traceId;
    this.finished = true;
    for (const span of this.spans.values()) {
      if (span.endTime === null) { // close anything the framework never ended
        span.set("tdn.unclosed", true);
        span.end();
      }
    }
    if (opts.output !== undefined) this.root.outputDigest = this.state.putBlob(opts.output);
    if (opts.error) this.root.error(opts.error);
    if (!("tdn.run.outcome" in this.root.attributes)) {
      this.root.set("tdn.run.outcome", this.root.statusCode === "ERROR" ? "failure" : "success");
    }
    this.root.end();
    void this.state.flush([...this.state.spans.filter(s => s !== this.root), this.root]);
    await this.state.drain();
    return this.traceId;
  }
}

/** The run state active in this async context, for adapters that add spans by hand. */
export function currentRun(): RunState | null {
  return ctx().run ?? null;
}

/** Internal: run `fn` as a child step of the current span (used by integrations). */
export const _withSpan = withSpan;

/**
 * Internal: open a child step of the current span that outlives the current call (a
 * streamed model response). Returns null outside a run. Close it with `_endSpan`.
 */
export function _startSpan(name: string, kind: SpanKind, attrs: Record<string, unknown>): { span: Span; state: RunState } | null {
  const c = ctx();
  const state = c.run;
  if (!state) return null;
  const parent = c.span ?? null;
  const span = new Span({ traceId: state.traceId, name, kind, parentSpanId: parent?.spanId ?? null });
  span.update(attrs);
  span.set("tdn.step", state.nextStep());
  const agentName = nearestAgent(state, parent);
  if (agentName && kind !== "agent") span.set("tdn.agent.name", agentName);
  state.spans.push(span);
  return { span, state };
}

export function _endSpan(h: { span: Span; state: RunState }, error?: unknown): void {
  if (error !== undefined && h.span.statusCode !== "ERROR") h.span.error(errorText(error));
  h.span.end();
  if (config.stream) void h.state.flush([h.span]);
}

/** Internal: the replay decision for a step span (used by integrations). */
export const _replayable = replayable;
