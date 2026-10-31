"use client";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { Pause, Play, Trash2 } from "lucide-react";
import { subscribe } from "@/lib/sse";
import { kindOf } from "@/lib/kinds";
import { short } from "@/lib/format";
import { PageHeader } from "@/components/page-header";
import { Badge, outcomeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

interface Item { id: number; at: number; type: string; data: Record<string, unknown> }
type SpanEv = { span_id: string; name: string; kind: string; status: { code: string }; step: number | null };

export function LivePage() {
  const [items, setItems] = useState<Item[]>([]);
  const [status, setStatus] = useState<"connecting" | "open" | "error">("connecting");
  const [paused, setPaused] = useState(false);
  const pausedRef = useRef(false);
  const seq = useRef(0);
  pausedRef.current = paused;

  useEffect(() => subscribe("/live", e => {
    if (pausedRef.current) return;
    setItems(prev => [{ id: ++seq.current, at: Date.now(), type: e.type, data: e.data as Record<string, unknown> }, ...prev].slice(0, 300));
  }, setStatus), []);

  const dot = status === "open" ? "bg-ok" : status === "connecting" ? "bg-warn" : "bg-bad";
  const label = status === "open" ? "Connected · waiting for events" : status === "connecting" ? "Connecting…" : "Disconnected · retrying";
  return (
    <>
      <PageHeader title="Live" sub="Steps as they arrive, finished runs and drift alerts, streamed from the server."
        action={<div className="flex items-center gap-2">
          <span className="flex items-center gap-2 text-sm text-muted" role="status"><span className={`h-2 w-2 rounded-full ${dot} ${status === "open" ? "animate-pulse" : ""}`} />{label}</span>
          <Button size="sm" onClick={() => setPaused(!paused)}>{paused ? <><Play size={13} />Resume</> : <><Pause size={13} />Pause</>}</Button>
          <Button size="sm" variant="ghost" onClick={() => setItems([])}><Trash2 size={13} />Clear</Button>
        </div>} />
      {!items.length ? (
        <div className="rounded-lg border border-dashed border-line px-6 py-12 text-center text-sm text-muted">
          No events yet. Run an agent that sends to this server and its steps appear here as they happen.
        </div>
      ) : (
        <ol className="space-y-1.5" aria-live="polite">
          {items.map(it => {
            const d = it.data;
            const time = new Date(it.at).toLocaleTimeString();
            if (it.type === "run") return (
              <li key={it.id} className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-panel px-3 py-2 text-sm">
                <span className="w-20 font-mono text-xs text-muted">{time}</span>
                <Badge tone={outcomeTone(String(d.outcome))}>{String(d.outcome)}</Badge>
                <Link href={`/runs/${d.trace_id}`} className="font-medium hover:underline underline-offset-2">{String(d.agent)}</Link>
                <span className="font-mono text-xs text-muted">{short(String(d.trace_id))}</span>
                {d.root_cause ? <span className="text-cause">· {String(d.root_cause)}</span> : null}
              </li>
            );
            if (it.type === "drift") return (
              <li key={it.id} className="flex flex-wrap items-center gap-2 rounded-lg border border-warn/40 bg-warn-soft/40 px-3 py-2 text-sm">
                <span className="w-20 font-mono text-xs text-muted">{time}</span><Badge tone="warn">drift</Badge>
                <Link href="/drift" className="hover:underline underline-offset-2">{String(d.headline)}</Link>
              </li>
            );
            if (it.type === "spans") {
              const spans = (d.spans as SpanEv[]) || [];
              return (
                <li key={it.id} className="flex flex-wrap items-center gap-2 px-3 py-1 text-xs text-muted">
                  <span className="w-20 font-mono">{time}</span>
                  <Link href={`/runs/${d.trace_id}`} className="font-mono hover:text-ink">{short(String(d.trace_id))}</Link>
                  {spans.slice(0, 8).map(s => { const k = kindOf(s.kind); return (
                    <span key={s.span_id} className="inline-flex items-center gap-1 rounded border border-line bg-panel px-1.5 py-0.5 text-ink">
                      <k.icon size={11} style={{ color: k.color }} aria-hidden />{s.step != null ? `#${s.step} ` : ""}{s.name.replace(/^(invoke_agent|execute_tool|chat)\s+/, "")}
                      {s.status?.code === "ERROR" && <span className="h-1.5 w-1.5 rounded-full bg-bad" aria-label="error" />}
                    </span>); })}
                  {spans.length > 8 && <span>+{spans.length - 8}</span>}
                </li>
              );
            }
            return <li key={it.id} className="px-3 py-1 font-mono text-xs text-muted">{time} {it.type} {JSON.stringify(d).slice(0, 160)}</li>;
          })}
        </ol>
      )}
    </>
  );
}
