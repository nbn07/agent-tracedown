"use client";
import { useState } from "react";
import type { Claims } from "@/lib/types";
import { Badge, type Tone } from "@/components/ui/badge";
import { pct } from "@/lib/format";

const LABEL_TONE: Record<string, Tone> = { supported: "ok", partial: "warn", stale: "warn", unsupported: "bad", contradicted: "bad", no_claim: "neutral" };

function supportColor(v: number | null) {
  if (v == null) return "transparent";
  // red (0) -> amber (0.5) -> green (1), as translucent highlights that work in both themes
  if (v >= 0.75) return "color-mix(in srgb, var(--ok) 28%, transparent)";
  if (v >= 0.4) return "color-mix(in srgb, var(--warn) 30%, transparent)";
  return "color-mix(in srgb, var(--bad) 30%, transparent)";
}

export function ClaimsView({ data, onStep }: { data: Claims; onStep: (spanId: string) => void }) {
  const [active, setActive] = useState<string | null>(null);
  if (!data.answer) return <p className="text-sm text-muted">This run has no final answer to check.</p>;
  const s = data.summary;
  // rebuild the answer: highlighted segments plus the untouched text between them
  const parts: { text: string; claim: string | null; support: number | null }[] = [];
  let pos = 0;
  for (const seg of [...data.segments].sort((a, b) => a.start - b.start)) {
    if (seg.start > pos) parts.push({ text: data.answer.slice(pos, seg.start), claim: null, support: null });
    parts.push({ text: data.answer.slice(seg.start, seg.end), claim: seg.claim, support: seg.support });
    pos = Math.max(pos, seg.end);
  }
  if (pos < data.answer.length) parts.push({ text: data.answer.slice(pos), claim: null, support: null });

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <div>
        <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
          <span className="eyebrow">Final answer</span>
          <span className="text-muted">unsupported or contradicted {pct(s.hallucination_score)} · {s.claims} claims</span>
        </div>
        <p className="rounded-md border border-line bg-bg p-3 text-[15px] leading-8">
          {parts.map((p, i) => (
            <span key={i} onMouseEnter={() => p.claim && setActive(p.claim)} onMouseLeave={() => setActive(null)}
              className={p.claim && active === p.claim ? "outline outline-1 outline-ink/40 rounded-sm" : ""}
              style={{ background: supportColor(p.support), borderRadius: 3 }}>{p.text}</span>
          ))}
        </p>
        <div className="mt-2 flex gap-3 text-xs text-muted">
          <span><span className="inline-block h-2.5 w-2.5 rounded-sm align-middle" style={{ background: supportColor(1) }} /> found in a source</span>
          <span><span className="inline-block h-2.5 w-2.5 rounded-sm align-middle" style={{ background: supportColor(0.5) }} /> partly</span>
          <span><span className="inline-block h-2.5 w-2.5 rounded-sm align-middle" style={{ background: supportColor(0) }} /> not in any source</span>
        </div>
      </div>
      <ul className="space-y-2">
        {data.claims.map(c => (
          <li key={c.id} onMouseEnter={() => setActive(c.id)} onMouseLeave={() => setActive(null)}
            className={`rounded-lg border p-3 text-sm ${active === c.id ? "border-ink/40" : "border-line"}`}>
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={LABEL_TONE[c.label] || "neutral"}>{c.label.replace("_", " ")}</Badge>
              <span className="font-medium">“{c.text}”</span>
            </div>
            {c.rationale && <p className="mt-1 text-xs text-muted">{c.rationale}</p>}
            <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-xs">
              {c.origin && <button className="text-accent underline underline-offset-2 cursor-pointer" onClick={() => onStep(c.origin!.span_id)}>written at step {c.origin.step}</button>}
              {c.evidence.slice(0, 3).map((e, i) => (
                <button key={i} className="text-muted hover:text-ink cursor-pointer" onClick={() => onStep(e.span_id)}>
                  {e.source} @ step {e.step} ({Math.round(e.score * 100)}%{e.stale ? ", stale" : ""})
                </button>
              ))}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
