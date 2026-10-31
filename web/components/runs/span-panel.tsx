"use client";
import { X } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { RootCause, Span } from "@/lib/types";
import { kindOf, shortName, stepOf } from "@/lib/kinds";
import { Badge } from "@/components/ui/badge";
import { ms, num, usd } from "@/lib/format";

function Payload({ label, digest, traceId }: { label: string; digest: string | null; traceId: string }) {
  const [state, setState] = useState<{ v?: unknown; err?: string; loading: boolean }>({ loading: Boolean(digest) });
  useEffect(() => {
    if (!digest) { setState({ loading: false }); return; }
    let alive = true;
    setState({ loading: true });
    api<{ content: unknown }>(`/blobs/${encodeURIComponent(digest)}?trace_id=${traceId}`)
      .then(b => alive && setState({ v: b.content, loading: false }))
      .catch(e => alive && setState({ err: e.status === 403 ? "You don't have permission to read payloads." : "Not captured.", loading: false }));
    return () => { alive = false; };
  }, [digest, traceId]);
  const text = state.v === undefined ? "" : typeof state.v === "string" ? state.v : JSON.stringify(state.v, null, 2);
  return (
    <div>
      <div className="eyebrow mb-1">{label}</div>
      {!digest ? <p className="text-xs text-muted">None recorded.</p> : state.loading ? <p className="text-xs text-muted">Loading…</p> :
        state.err ? <p className="text-xs text-muted">{state.err}</p> :
          <pre className="max-h-72 overflow-auto rounded-md bg-sunken p-2.5 font-mono text-xs whitespace-pre-wrap break-words">{text}</pre>}
    </div>
  );
}

export function SpanPanel({ span, findings, onClose }: { span: Span; findings: RootCause[]; onClose: () => void }) {
  const k = kindOf(span.kind);
  const mine = findings.filter(f => f.span_id === span.span_id);
  const attrs = Object.entries(span.attributes).filter(([key]) => !key.startsWith("tdn.tools.available"));
  return (
    <aside className="rounded-lg border border-line bg-panel p-4 space-y-4" aria-label={`Step ${stepOf(span)} details`}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="eyebrow flex items-center gap-1.5"><k.icon size={12} style={{ color: k.color }} />{k.label} · step {stepOf(span)}</div>
          <h3 className="mt-0.5 text-lg font-semibold break-words">{shortName(span.name)}</h3>
        </div>
        <button onClick={onClose} className="rounded p-1 text-muted hover:text-ink cursor-pointer" aria-label="Close step details"><X size={16} /></button>
      </div>
      <div className="flex flex-wrap gap-2 text-xs">
        <Badge tone={span.status.code === "ERROR" ? "bad" : "ok"}>{span.status.code === "ERROR" ? "error" : "ok"}</Badge>
        <span className="text-muted tabular">{ms(((span.end_time || span.start_time) - span.start_time) / 1e6)}</span>
        {span.kind === "llm" && (
          <span className="text-muted tabular" title={span.cost_source === "reported" ? "Cost reported by the caller" : span.cost_source === "computed" ? "Tokens × the model's list price" : "No known price for this model"}>
            {num(Number(span.attributes["gen_ai.usage.input_tokens"] ?? 0))} in · {num(Number(span.attributes["gen_ai.usage.output_tokens"] ?? 0))} out · {span.cost_usd != null ? usd(span.cost_usd) : "no price"}
          </span>
        )}
        {mine.map(f => <Badge key={f.id} tone={f.root_cause ? "cause" : "neutral"}>{f.label}</Badge>)}
      </div>
      {span.status.message && <p className="rounded-md bg-bad-soft px-2.5 py-1.5 text-xs text-bad break-words">{span.status.message}</p>}
      <Payload label="Input" digest={span.input_digest} traceId={span.trace_id} />
      <Payload label="Output" digest={span.output_digest} traceId={span.trace_id} />
      <details>
        <summary className="cursor-pointer text-xs text-muted">Attributes ({attrs.length})</summary>
        <dl className="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 font-mono text-[11px]">
          {attrs.map(([key, v]) => (
            <div key={key} className="contents"><dt className="text-muted">{key}</dt><dd className="break-words">{typeof v === "string" ? v : JSON.stringify(v)}</dd></div>
          ))}
        </dl>
      </details>
    </aside>
  );
}
