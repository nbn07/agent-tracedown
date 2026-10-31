/**
 * Span model for the Tracedown telemetry spec v0.1 (same wire format as the Python SDK).
 *
 * A span is one step of an agent run. Field names follow OpenTelemetry; `gen_ai.*`
 * attributes follow the OTel GenAI semantic conventions and `tdn.*` attributes add
 * what debugging and replay need.
 */
import { randomBytes, createHash } from "node:crypto";

/** Tracedown span kind -> gen_ai.operation.name (null = Tracedown extension). */
export const KIND_TO_OPERATION = {
  agent: "invoke_agent",
  llm: "chat",
  tool: "execute_tool",
  retrieval: "retrieval",
  embedding: "embeddings",
  memory: null,
  decision: null,
  handoff: "invoke_agent",
} as const;

export type SpanKind = keyof typeof KIND_TO_OPERATION;
export const SPAN_KINDS = Object.keys(KIND_TO_OPERATION) as SpanKind[];
export type StatusCode = "UNSET" | "OK" | "ERROR";

export interface SpanDict {
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  name: string;
  kind: SpanKind;
  start_time: number;
  end_time: number | null;
  status: { code: StatusCode; message: string };
  attributes: Record<string, unknown>;
  input_digest: string | null;
  output_digest: string | null;
}

export const newTraceId = (): string => randomBytes(16).toString("hex");
export const newSpanId = (): string => randomBytes(8).toString("hex");

// Nanosecond wall clock. Date.now() has ms resolution; add a monotonic sub-ms part so
// steps that start in the same millisecond still sort in order.
const originMs = Date.now();
const originHr = process.hrtime.bigint();
export function nowNs(): number {
  const elapsed = process.hrtime.bigint() - originHr;
  return Number(BigInt(originMs) * 1_000_000n + elapsed);
}

/**
 * Serialize like Python's `json.dumps(value, sort_keys=True, default=str)`, so a payload
 * gets the same content digest whichever SDK recorded it: sorted keys, ", " and ": "
 * separators, non-ASCII escaped as \uXXXX.
 */
export function canonicalJson(value: unknown): string {
  return encode(value, new Set());
}

function encode(v: unknown, seen: Set<unknown>): string {
  if (v === null || v === undefined) return "null";
  switch (typeof v) {
    case "string": return asciiString(v);
    case "boolean": return v ? "true" : "false";
    case "number":
      if (Number.isNaN(v)) return "NaN";
      if (!Number.isFinite(v)) return v > 0 ? "Infinity" : "-Infinity";
      return JSON.stringify(v);
    case "bigint": return v.toString();
    case "function": case "symbol": return asciiString(String(v));
  }
  if (v instanceof Date) return asciiString(v.toISOString());
  if (typeof (v as { toJSON?: unknown }).toJSON === "function") {
    return encode((v as { toJSON: () => unknown }).toJSON(), seen);
  }
  if (seen.has(v)) return asciiString("[Circular]");
  seen.add(v);
  try {
    if (Array.isArray(v)) return "[" + v.map(x => encode(x === undefined ? null : x, seen)).join(", ") + "]";
    if (v instanceof Map) v = Object.fromEntries(v);
    if (v instanceof Set) return "[" + [...v].map(x => encode(x, seen)).join(", ") + "]";
    const obj = v as Record<string, unknown>;
    const keys = Object.keys(obj).filter(k => obj[k] !== undefined && typeof obj[k] !== "function").sort(pyKeyOrder);
    return "{" + keys.map(k => asciiString(k) + ": " + encode(obj[k], seen)).join(", ") + "}";
  } finally {
    seen.delete(v);
  }
}

// Python sorts str keys by code point; JS default sort compares UTF-16 units. They only
// differ for astral characters, which matters little, but match exactly anyway.
function pyKeyOrder(a: string, b: string): number {
  const ca = [...a], cb = [...b];
  for (let i = 0; i < Math.min(ca.length, cb.length); i++) {
    const d = ca[i].codePointAt(0)! - cb[i].codePointAt(0)!;
    if (d) return d;
  }
  return ca.length - cb.length;
}

function asciiString(s: string): string {
  return JSON.stringify(s).replace(/[\u0080-￿]/g, c => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
}

export function digestOf(text: string): string {
  return "sha256:" + createHash("sha256").update(text, "utf8").digest("hex");
}

export class Span {
  readonly traceId: string;
  readonly spanId: string;
  name: string;
  readonly kind: SpanKind;
  parentSpanId: string | null;
  startTime: number = nowNs();
  endTime: number | null = null;
  statusCode: StatusCode = "UNSET";
  statusMessage = "";
  attributes: Record<string, unknown> = {};
  inputDigest: string | null = null;
  outputDigest: string | null = null;

  constructor(opts: { traceId: string; name: string; kind: SpanKind; parentSpanId?: string | null; spanId?: string }) {
    if (!SPAN_KINDS.includes(opts.kind)) {
      throw new Error(`unknown span kind ${JSON.stringify(opts.kind)}; expected one of ${SPAN_KINDS.join(", ")}`);
    }
    this.traceId = opts.traceId;
    this.spanId = opts.spanId ?? newSpanId();
    this.name = opts.name;
    this.kind = opts.kind;
    this.parentSpanId = opts.parentSpanId ?? null;
    const op = KIND_TO_OPERATION[opts.kind];
    if (op) this.attributes["gen_ai.operation.name"] = op;
  }

  set(key: string, value: unknown): this {
    this.attributes[key] = value;
    return this;
  }

  /** Set several attributes, skipping null/undefined values. */
  update(attrs: Record<string, unknown> | undefined): this {
    for (const [k, v] of Object.entries(attrs ?? {})) if (v !== undefined && v !== null) this.attributes[k] = v;
    return this;
  }

  error(message: string): void {
    this.statusCode = "ERROR";
    this.statusMessage = message;
  }

  end(): void {
    if (this.endTime === null) this.endTime = nowNs();
    if (this.statusCode === "UNSET") this.statusCode = "OK";
  }

  toDict(): SpanDict {
    return {
      trace_id: this.traceId,
      span_id: this.spanId,
      parent_span_id: this.parentSpanId,
      name: this.name,
      kind: this.kind,
      start_time: this.startTime,
      end_time: this.endTime,
      status: { code: this.statusCode, message: this.statusMessage },
      attributes: this.attributes,
      input_digest: this.inputDigest,
      output_digest: this.outputDigest,
    };
  }
}
