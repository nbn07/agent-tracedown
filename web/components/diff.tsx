"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useMemo, useState } from "react";
import { useApi } from "@/lib/api";
import type { Diff, RunSummary } from "@/lib/types";
import { ago, short } from "@/lib/format";
import { kindOf } from "@/lib/kinds";
import { PageHeader } from "@/components/page-header";
import { Badge, outcomeTone, type Tone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { ErrorState, Loading } from "@/components/ui/states";

const changeTone: Record<string, Tone> = { same: "neutral", output: "warn", status: "bad", added: "accent", removed: "neutral", changed: "warn" };

function Cell({ s }: { s: Diff["steps"][number]["a"] }) {
  if (!s) return <td className="px-3 py-2 align-top text-xs text-muted">—</td>;
  const k = kindOf(s.kind);
  return (
    <td className="px-3 py-2 align-top">
      <div className="flex items-center gap-1.5 text-sm">
        <k.icon size={12} style={{ color: k.color }} aria-hidden />
        <span className="truncate">{s.name}</span>
        {s.status === "ERROR" && <Badge tone="bad">error</Badge>}
      </div>
      {s.output && <pre className="mt-1 max-h-24 overflow-auto whitespace-pre-wrap break-words rounded bg-sunken p-1.5 font-mono text-[11px]">{s.output}</pre>}
    </td>
  );
}

export function DiffPage() {
  const params = useSearchParams();
  const router = useRouter();
  const a = params.get("a"), b = params.get("b");
  const diff = useApi<Diff>(a && b ? `/diff?a=${a}&b=${b}` : null);
  const runs = useApi<{ runs: RunSummary[] }>("/runs?limit=200");
  const [pa, setPa] = useState(""); const [pb, setPb] = useState("");
  const pairs = useMemo(() => (runs.data?.runs || []).filter(r => r.replay_of), [runs.data]);
  const all = runs.data?.runs || [];

  return (
    <>
      <PageHeader title="Compare runs" sub="Step-by-step differences between two runs, typically a failed run and its replay with a fix." />
      {(!a || !b) && (
        <div className="space-y-4">
          {pairs.length > 0 && (
            <div>
              <div className="eyebrow mb-2">Replays and their originals</div>
              <ul className="space-y-1.5">{pairs.map(r => (
                <li key={r.trace_id}><Link href={`/diff?a=${r.replay_of}&b=${r.trace_id}`} className="inline-flex flex-wrap items-center gap-2 rounded-lg border border-line bg-panel px-3 py-2 text-sm hover:border-ink/30">
                  <span className="font-medium">{r.agent_name}</span><span className="font-mono text-xs text-muted">{short(r.replay_of)} → {short(r.trace_id)}</span>
                  <Badge tone={outcomeTone(r.outcome)}>replay {r.outcome}</Badge><span className="text-xs text-muted">{ago(r.started_at)}</span>
                </Link></li>
              ))}</ul>
            </div>
          )}
          <div className="grid max-w-3xl grid-cols-1 gap-3 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
            <Select label="Run A" value={pa} onChange={e => setPa(e.target.value)}><option value="">Choose a run</option>{all.map(r => <option key={r.trace_id} value={r.trace_id}>{r.agent_name} · {short(r.trace_id)} · {r.outcome}</option>)}</Select>
            <Select label="Run B" value={pb} onChange={e => setPb(e.target.value)}><option value="">Choose a run</option>{all.map(r => <option key={r.trace_id} value={r.trace_id}>{r.agent_name} · {short(r.trace_id)} · {r.outcome}</option>)}</Select>
            <Button disabled={!pa || !pb} onClick={() => router.push(`/diff?a=${pa}&b=${pb}`)}>Compare</Button>
          </div>
        </div>
      )}
      {a && b && (diff.error ? <ErrorState error={diff.error} onRetry={diff.reload} /> : !diff.data ? <Loading rows={6} /> : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <Link href={`/runs/${a}`} className="font-mono underline underline-offset-2">{short(a)}</Link><Badge tone={outcomeTone(diff.data.outcome_a)}>{diff.data.outcome_a}</Badge>
            <span className="text-muted">→</span>
            <Link href={`/runs/${b}`} className="font-mono underline underline-offset-2">{short(b)}</Link><Badge tone={outcomeTone(diff.data.outcome_b)}>{diff.data.outcome_b}</Badge>
            <span className="text-muted">first difference at step <b className="text-ink">{diff.data.first_divergence ?? "—"}</b></span>
          </div>
          <div className="overflow-x-auto rounded-lg border border-line bg-panel">
            <table className="w-full min-w-[720px] text-sm table-fixed">
              <thead className="text-left text-xs text-muted"><tr className="border-b border-line"><th className="w-14 px-3 py-2 font-medium">Step</th><th className="w-24 px-3 py-2 font-medium">Change</th><th className="px-3 py-2 font-medium">Run A</th><th className="px-3 py-2 font-medium">Run B</th></tr></thead>
              <tbody>{diff.data.steps.map(r => (
                <tr key={r.step} className={`border-b border-line last:border-0 ${r.step === diff.data!.first_divergence ? "bg-cause-soft/40" : ""}`}>
                  <td className="px-3 py-2 align-top font-mono text-xs tabular">{r.step}</td>
                  <td className="px-3 py-2 align-top"><Badge tone={changeTone[r.change] || "neutral"}>{r.change}</Badge></td>
                  <Cell s={r.a} /><Cell s={r.b} />
                </tr>
              ))}</tbody>
            </table>
          </div>
        </div>
      ))}
    </>
  );
}
