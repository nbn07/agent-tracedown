/**
 * Exporters ship finished runs (spans + content blobs) somewhere. No dependencies:
 * they use the global `fetch` of Node 18+.
 */
import type { SpanDict } from "./spans.js";
import { SDK_VERSION } from "./version.js";

export type Blobs = Record<string, string>;

export interface Exporter {
  export(spans: SpanDict[], blobs: Blobs): Promise<void> | void;
}

/** Keeps runs in memory. Used by tests and by replay. */
export class MemoryExporter implements Exporter {
  spans: SpanDict[] = [];
  blobs: Blobs = {};
  export(spans: SpanDict[], blobs: Blobs): void {
    this.spans.push(...spans);
    Object.assign(this.blobs, blobs);
  }
  trace(traceId: string): SpanDict[] {
    return this.spans.filter(s => s.trace_id === traceId);
  }
  /** The run as a replay bundle: `{spans, blobs}`. */
  bundle(traceId: string): { spans: SpanDict[]; blobs: Blobs } {
    return { spans: this.trace(traceId), blobs: { ...this.blobs } };
  }
  clear(): void {
    this.spans = [];
    this.blobs = {};
  }
}

/** Prints a compact tree of each run to stderr. */
export class ConsoleExporter implements Exporter {
  constructor(private write: (line: string) => void = l => process.stderr.write(l + "\n")) {}
  export(spans: SpanDict[]): void {
    const children = new Map<string | null, SpanDict[]>();
    for (const s of spans) {
      const list = children.get(s.parent_span_id) ?? [];
      list.push(s);
      children.set(s.parent_span_id, list);
    }
    const walk = (parent: string | null, depth: number) => {
      for (const s of (children.get(parent) ?? []).sort((a, b) => a.start_time - b.start_time)) {
        const ms = ((s.end_time ?? s.start_time) - s.start_time) / 1e6;
        const mark = s.status.code === "ERROR" ? "x" : "-";
        const step = s.attributes["tdn.step"] ?? "";
        this.write(`${"  ".repeat(depth)}${mark} [${step}] ${s.kind.padEnd(9)} ${s.name}  ${ms.toFixed(1)}ms`);
        walk(s.span_id, depth + 1);
      }
    };
    walk(null, 0);
  }
}

/** Posts spans and blobs to a Tracedown server's ingest API. Never throws. */
export class HttpExporter implements Exporter {
  readonly endpoint: string;
  lastError: string | null = null;
  constructor(endpoint: string, readonly opts: { apiKey?: string; project?: string; timeoutMs?: number } = {}) {
    this.endpoint = endpoint.replace(/\/+$/, "");
  }

  private async post(path: string, payload: unknown): Promise<void> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (this.opts.apiKey) headers.Authorization = `Bearer ${this.opts.apiKey}`;
    const resp = await fetch(this.endpoint + path, {
      method: "POST", headers, body: JSON.stringify(payload),
      signal: AbortSignal.timeout(this.opts.timeoutMs ?? 5000),
    });
    await resp.arrayBuffer();
    if (!resp.ok) throw new Error(`HTTP ${resp.status} from ${path}`);
  }

  async export(spans: SpanDict[], blobs: Blobs): Promise<void> {
    // Telemetry must never crash the agent: failures are recorded, not raised.
    const project = this.opts.project ?? "default";
    try {
      if (Object.keys(blobs).length) await this.post("/v1/ingest/blobs", { project, blobs });
      await this.post("/v1/ingest/spans", { project, spans });
      this.lastError = null;
    } catch (err) {
      this.lastError = String(err);
      process.stderr.write(`[agent-tracedown] export failed: ${err}\n`);
    }
  }
}

/**
 * Send runs straight to any OTLP/HTTP endpoint (OpenTelemetry Collector, Jaeger,
 * Grafana Tempo, Datadog Agent). Payload content is not sent, only digests; pair with
 * `HttpExporter` via `FanoutExporter` to keep replay and root-cause analysis.
 */
export class OtlpHttpExporter implements Exporter {
  lastError: string | null = null;
  constructor(readonly endpoint = "http://localhost:4318/v1/traces",
              readonly opts: { headers?: Record<string, string>; serviceName?: string; timeoutMs?: number } = {}) {}
  async export(spans: SpanDict[]): Promise<void> {
    try {
      const resp = await fetch(this.endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...(this.opts.headers ?? {}) },
        body: JSON.stringify(toOtlpJson(spans, this.opts.serviceName ?? "agent")),
        signal: AbortSignal.timeout(this.opts.timeoutMs ?? 5000),
      });
      await resp.arrayBuffer();
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      this.lastError = null;
    } catch (err) {
      this.lastError = String(err);
      process.stderr.write(`[agent-tracedown] OTLP export failed: ${err}\n`);
    }
  }
}

/** Send every run to several exporters (e.g. Tracedown server + OTLP collector). */
export class FanoutExporter implements Exporter {
  readonly exporters: Exporter[];
  constructor(...exporters: Exporter[]) { this.exporters = exporters; }
  async export(spans: SpanDict[], blobs: Blobs): Promise<void> {
    await Promise.all(this.exporters.map(e => e.export(spans, blobs)));
  }
}

/** Convert Tracedown spans to an OTLP/JSON ExportTraceServiceRequest body. */
export function toOtlpJson(spans: SpanDict[], serviceName = "agent"): Record<string, unknown> {
  const attr = (key: string, v: unknown) => {
    let value: Record<string, unknown>;
    if (typeof v === "boolean") value = { boolValue: v };
    else if (typeof v === "number" && Number.isInteger(v)) value = { intValue: String(v) };
    else if (typeof v === "number") value = { doubleValue: v };
    else value = { stringValue: typeof v === "string" ? v : JSON.stringify(v) };
    return { key, value };
  };
  const statusMap = { UNSET: 0, OK: 1, ERROR: 2 } as const;
  const otlpSpans = spans.map(s => {
    const attrs: Record<string, unknown> = { ...s.attributes, "tdn.kind": s.kind };
    if (s.input_digest) attrs["tdn.input.digest"] = s.input_digest;
    if (s.output_digest) attrs["tdn.output.digest"] = s.output_digest;
    return {
      traceId: s.trace_id,
      spanId: s.span_id,
      parentSpanId: s.parent_span_id ?? "",
      name: s.name,
      kind: ["agent", "decision", "memory"].includes(s.kind) ? 1 : 3,
      startTimeUnixNano: String(s.start_time),
      endTimeUnixNano: String(s.end_time ?? s.start_time),
      attributes: Object.entries(attrs).map(([k, v]) => attr(k, v)),
      status: { code: statusMap[s.status.code], message: s.status.message },
    };
  });
  return {
    resourceSpans: [{
      resource: { attributes: [attr("service.name", serviceName)] },
      scopeSpans: [{ scope: { name: "agent-tracedown", version: SDK_VERSION }, spans: otlpSpans }],
    }],
  };
}
