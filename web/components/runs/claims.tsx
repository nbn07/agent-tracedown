"use client";
import { useState } from "react";
import type { Claim, Claims } from "@/lib/types";
import { Badge, type Tone } from "@/components/ui/badge";
import { pct } from "@/lib/format";

const LABEL_TONE: Record<string, Tone> = { supported: "ok", partial: "warn", stale: "warn", unsupported: "bad", contradicted: "bad", no_claim: "neutral" };
const LABEL_TEXT: Record<string, string> = {
  supported: "supported", partial: "partly supported", stale: "outdated source", unsupported: "no source",
  contradicted: "contradicted", no_claim: "not a claim",
};

// Highlight per claim label, as translucent fills that work in both themes.
function labelColor(label: string | undefined) {
  switch (label) {
    case "supported": return "color-mix(in srgb, var(--ok) 24%, transparent)";
    case "partial": case "stale": return "color-mix(in srgb, var(--warn) 30%, transparent)";
    case "unsupported": case "contradicted": return "color-mix(in srgb, var(--bad) 28%, transparent)";
    default: return "transparent";
  }
}

/** Where a claim came from, in one line: the best source, its step, and the step that wrote it. */
function blame(c: Claim) {
  const best = c.evidence[0];
  if (c.label === "no_claim") return { source: "", where: "", detail: "" };
  if (!best || c.label === "unsupported") {
    return { source: "no source", where: c.origin ? `model, step ${c.origin.step}` : "",
      detail: c.origin ? `first written by the model at step ${c.origin.step}` : "" };
  }
  const via = c.origin ? `written at step ${c.origin.step}` : "";
  return { source: best.source, where: `step ${best.step}${best.stale ? " · outdated" : ""}`,
    detail: [c.label === "contradicted" ? "the source says otherwise" : "", via].filter(Boolean).join(" · ") };
}

export function ClaimsView({ data, onStep }: { data: Claims; onStep: (spanId: string) => void }) {
  const [active, setActive] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  if (!data.answer) return <p className="text-sm text-muted">This run has no final answer to check.</p>;
  const s = data.summary;
  const byId = Object.fromEntries(data.claims.map(c => [c.id, c]));
  const ordered = [...data.claims].sort((a, b) => a.start - b.start);
  const shown = ordered.filter(c => c.label !== "no_claim");

  // the answer, each claim painted by its label
  const parts: { text: string; claim: string | null }[] = [];
  let pos = 0;
  for (const c of ordered) {
    if (c.start < pos) continue;
    if (c.start > pos) parts.push({ text: data.answer.slice(pos, c.start), claim: null });
    parts.push({ text: data.answer.slice(c.start, c.end), claim: c.id });
    pos = c.end;
  }
  if (pos < data.answer.length) parts.push({ text: data.answer.slice(pos), claim: null });

  return (
    <div className="space-y-4">
      <div>
        <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
          <span className="eyebrow">Final answer</span>
          <span className="text-muted">{s.claims} claims · {s.supported} supported · {s.stale} outdated · {s.unsupported + s.contradicted} unsupported or contradicted ({pct(s.hallucination_score)})</span>
        </div>
        <p className="rounded-md border border-line bg-bg p-3 text-[15px] leading-8">
          {parts.map((p, i) => {
            const c = p.claim ? byId[p.claim] : undefined;
            return (
              <span key={i} onMouseEnter={() => c && setActive(c.id)} onMouseLeave={() => setActive(null)}
                onClick={() => c && setOpen(open === c.id ? null : c.id)}
                className={`${c ? "cursor-pointer" : ""} ${c && active === c.id ? "outline outline-1 outline-ink/40" : ""}`}
                style={{ background: labelColor(c?.label), borderRadius: 3,
                  textDecoration: c?.label === "stale" ? "underline dashed" : undefined }}>{p.text}</span>
            );
          })}
        </p>
      </div>

      <div>
        <div className="mb-2 flex items-center justify-between gap-2">
          <span className="eyebrow">Where each claim came from</span>
          <span className="text-xs text-muted">Click a row for the evidence</span>
        </div>
        <div className="overflow-hidden rounded-md border border-line">
          {shown.length === 0 && <p className="p-3 text-sm text-muted">No checkable claims in this answer.</p>}
          {shown.map(c => {
            const b = blame(c);
            const target = c.origin?.span_id || c.evidence[0]?.span_id;
            return (
              <div key={c.id} onMouseEnter={() => setActive(c.id)} onMouseLeave={() => setActive(null)}
                className={`border-b border-line last:border-b-0 ${active === c.id ? "bg-ink/[0.03]" : ""}`}>
                <button onClick={() => setOpen(open === c.id ? null : c.id)}
                  className="grid w-full cursor-pointer grid-cols-1 gap-x-4 gap-y-1 px-3 py-2 text-left md:grid-cols-[minmax(0,15rem)_minmax(0,1fr)]">
                  <span className="min-w-0 font-mono text-xs leading-6 text-muted">
                    <span className="block truncate" title={b.source}>{b.source}</span>
                    {b.where && <span className="block text-[11px] leading-4">{b.where}</span>}
                  </span>
                  <span className="min-w-0 text-sm leading-6">
                    <span className="mr-2 inline-block h-2.5 w-2.5 rounded-sm align-middle" style={{ background: labelColor(c.label) }} />
                    {c.text}
                    <Badge tone={LABEL_TONE[c.label] || "neutral"} className="ml-2 align-middle">{LABEL_TEXT[c.label] || c.label}</Badge>
                  </span>
                </button>
                {open === c.id && (
                  <div className="space-y-1.5 px-3 pb-3 text-xs md:pl-[calc(15rem+1.75rem)]">
                    {b.detail && <p className="text-muted">{b.detail}</p>}
                    {c.rationale && <p className="text-muted">{c.rationale}</p>}
                    {c.evidence.slice(0, 3).map((e, i) => (
                      <button key={i} className="block cursor-pointer text-left text-muted hover:text-ink" onClick={() => onStep(e.span_id)}>
                        {e.source} at step {e.step}: {Math.round(e.score * 100)}% match{e.stale ? ", outdated" : ""}
                        {e.matched.length > 0 && <> · shares “{e.matched.slice(0, 5).join("”, “")}”</>}
                      </button>
                    ))}
                    {target && <button className="text-accent underline underline-offset-2 cursor-pointer" onClick={() => onStep(target)}>Show in graph</button>}
                  </div>
                )}
              </div>
            );
          })}
        </div>
        <div className="mt-2 flex flex-wrap gap-3 text-xs text-muted">
          <span><span className="inline-block h-2.5 w-2.5 rounded-sm align-middle" style={{ background: labelColor("supported") }} /> found in a source</span>
          <span><span className="inline-block h-2.5 w-2.5 rounded-sm align-middle" style={{ background: labelColor("stale") }} /> partly, or from an outdated source</span>
          <span><span className="inline-block h-2.5 w-2.5 rounded-sm align-middle" style={{ background: labelColor("unsupported") }} /> in no source, or contradicted</span>
        </div>
      </div>
    </div>
  );
}
