"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { api, useApi } from "@/lib/api";
import type { DriftReport, RunSummary } from "@/lib/types";
import { ago, humanize, num, pct, short } from "@/lib/format";
import { PageHeader } from "@/components/page-header";
import { Badge, severityTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Panel } from "@/components/ui/panel";
import { Select } from "@/components/ui/select";
import { Empty, ErrorState, Loading } from "@/components/ui/states";

type R = DriftReport["report"] & { agent: string; dimension: string; baseline: string; candidate: string; verdict: string; severity: string };
const DIMS = ["prompt_version", "model", "tool_schema", "kb_snapshot", "release"];

function Change({ v }: { v: number | undefined }) {
  if (v == null) return <span className="text-muted">—</span>;
  const up = v > 0;
  return <span className={`tabular ${Math.abs(v) < 0.05 ? "text-muted" : up ? "text-warn" : "text-ok"}`}>{up ? "+" : ""}{(v * 100).toFixed(0)}%</span>;
}

function ReportView({ r }: { r: R }) {
  const sr = r.success_rate;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={r.verdict === "drift" ? "bad" : r.verdict === "insufficient_data" ? "neutral" : "ok"}>{humanize(r.verdict)}</Badge>
        <Badge tone={severityTone(r.severity)}>{r.severity}</Badge>
        <span className="text-sm text-muted">{r.agent} · {humanize(r.dimension)}: <b className="text-ink">{r.baseline}</b> → <b className="text-ink">{r.candidate}</b></span>
      </div>
      <ul className="list-disc space-y-0.5 pl-5 text-sm">{(r.signals || []).map((s, i) => <li key={i}>{s}</li>)}</ul>
      {sr && (
        <div>
          <div className="eyebrow mb-2">Success rate</div>
          {[{ k: r.baseline, v: sr.a, n: r.runs?.a }, { k: r.candidate, v: sr.b, n: r.runs?.b }].map((x, i) => (
            <div key={i} className="mb-1.5 grid grid-cols-[minmax(0,140px)_1fr_auto] items-center gap-3 text-sm">
              <span className="truncate font-mono text-xs" title={x.k}>{x.k}</span>
              <div className="h-3 overflow-hidden rounded bg-sunken"><div className="h-full rounded" style={{ width: `${(x.v ?? 0) * 100}%`, background: i ? "var(--cause)" : "var(--ok)" }} /></div>
              <span className="tabular w-24 text-right">{pct(x.v)} <span className="text-xs text-muted">({x.n} runs)</span></span>
            </div>
          ))}
          {sr.p_value != null && <p className="text-xs text-muted">p {sr.p_value < 1e-6 ? "< 0.000001" : `= ${sr.p_value.toPrecision(2)}`} (two-proportion z-test)</p>}
        </div>
      )}
      <div className="grid gap-3 sm:grid-cols-3">
        {(["steps", "tokens", "duration_ms"] as const).map(k => r[k] ? (
          <div key={k} className="rounded-md border border-line px-3 py-2 text-sm">
            <div className="eyebrow">{k === "duration_ms" ? "duration" : k} per run</div>
            <div className="tabular">{num(r[k]!.a)} → {num(r[k]!.b)}{k === "duration_ms" ? " ms" : ""} <Change v={r[k]!.change} /></div>
          </div>
        ) : null)}
      </div>
      {r.tool_mix && (
        <div className="overflow-x-auto">
          <div className="eyebrow mb-2">Tool calls per run (JSD {r.tool_mix.jsd.toFixed(2)})</div>
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-muted"><tr><th className="py-1 font-medium">Tool</th><th className="py-1 text-right font-medium">{short(r.baseline, 14)}</th><th className="py-1 text-right font-medium">{short(r.candidate, 14)}</th><th className="py-1 text-right font-medium">Errors (new)</th><th className="py-1 font-medium pl-3">Change</th></tr></thead>
            <tbody>{r.tool_mix.tools.map(t => (
              <tr key={t.tool} className="border-t border-line"><td className="py-1.5">{t.tool}</td><td className="py-1.5 text-right tabular">{t.calls_per_run_a.toFixed(2)}</td><td className="py-1.5 text-right tabular">{t.calls_per_run_b.toFixed(2)}</td>
                <td className="py-1.5 text-right tabular">{pct(t.error_rate_b)}</td><td className="py-1.5 pl-3"><Badge tone={t.status === "new" ? "warn" : t.status === "gone" ? "neutral" : "neutral"}>{t.status}</Badge></td></tr>
            ))}</tbody>
          </table>
        </div>
      )}
      {r.root_causes && Object.keys(r.root_causes.b || {}).length > 0 && (
        <div><div className="eyebrow mb-1">Failure causes in {r.candidate}</div>
          <div className="flex flex-wrap gap-2">{Object.entries(r.root_causes.b).map(([k, v]) => <Badge key={k} tone="cause">{humanize(k)} · {v}</Badge>)}</div></div>
      )}
      {r.examples && (
        <div className="text-sm"><span className="eyebrow mr-2">Example runs</span>
          {(r.examples.b || []).map(t => <Link key={t} href={`/runs/${t}`} className="mr-2 font-mono text-xs text-accent underline underline-offset-2">{short(t)}</Link>)}</div>
      )}
    </div>
  );
}

export function DriftPage() {
  const params = useSearchParams();
  const router = useRouter();
  const { data, error, loading, reload } = useApi<{ reports: DriftReport[] }>("/drift/reports");
  const reports = data?.reports || [];
  const selectedId = Number(params.get("id")) || reports[0]?.id;
  const sel = reports.find(r => r.id === selectedId);

  // compare any two cohorts
  const facets = useApi<{ projects: string[]; agents: string[] }>("/facets");
  const agents = facets.data?.agents || [];
  const [agent, setAgent] = useState("");
  const [dim, setDim] = useState("prompt_version");
  const vals = useApi<{ values: { value: string; runs: number }[] }>(agent ? `/drift/values?agent=${encodeURIComponent(agent)}&dimension=${dim}` : null);
  const [a, setA] = useState(""); const [b, setB] = useState("");
  const [cmp, setCmp] = useState<R | null>(null); const [cmpErr, setCmpErr] = useState<string | null>(null);
  useEffect(() => { if (!agent && agents.length) setAgent(agents.includes("SupportBot") ? "SupportBot" : agents[0]); }, [agents, agent]);
  useEffect(() => { const v = vals.data?.values || []; setA(v[0]?.value || ""); setB(v[1]?.value || v[0]?.value || ""); setCmp(null); }, [vals.data]);
  const compare = () => api<R>(`/drift/compare?agent=${encodeURIComponent(agent)}&dimension=${dim}&a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`)
    .then(r => { setCmp(r); setCmpErr(null); }).catch(e => setCmpErr(e.message));

  return (
    <>
      <PageHeader title="Drift" sub="Did a new prompt, model, tool schema or knowledge-base snapshot change how the agent behaves? Reports are written automatically once a new version has enough runs." />
      {error ? <ErrorState error={error} onRetry={reload} /> : loading && !data ? <Loading rows={5} /> : (
        <div className="grid gap-4 lg:grid-cols-[340px_minmax(0,1fr)]">
          <div className="space-y-2">
            {!reports.length && <Empty title="No drift reports yet">They appear when a new version of a prompt, model or tool schema has enough runs to compare.</Empty>}
            {reports.map(r => (
              <button key={r.id} onClick={() => router.replace(`/drift?id=${r.id}`)}
                className={`block w-full rounded-lg border px-3 py-2.5 text-left cursor-pointer ${r.id === selectedId ? "border-ink/40 bg-panel" : "border-line bg-panel/60 hover:bg-panel"}`}>
                <div className="flex items-center gap-2 text-xs">
                  <Badge tone={r.verdict === "drift" ? "bad" : "ok"}>{humanize(r.verdict)}</Badge><Badge tone={severityTone(r.severity)}>{r.severity}</Badge>
                  <span className="ml-auto text-muted">{ago(r.created_at)}</span>
                </div>
                <div className="mt-1 text-sm font-medium">{r.agent} · {humanize(r.dimension)}</div>
                <div className="truncate font-mono text-xs text-muted">{r.baseline} → {r.candidate}</div>
              </button>
            ))}
          </div>
          <div className="space-y-4">
            {sel && <Panel title={sel.report.headline.split(":")[0]} eyebrow={`Report #${sel.id}`}><ReportView r={{ ...sel.report, agent: sel.agent, dimension: sel.dimension, baseline: sel.baseline, candidate: sel.candidate, verdict: sel.verdict, severity: sel.severity }} /></Panel>}
            <Panel title="Compare any two versions" eyebrow="On demand">
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-5 sm:items-end">
                <Select label="Agent" value={agent} onChange={e => setAgent(e.target.value)}>{agents.map(x => <option key={x}>{x}</option>)}</Select>
                <Select label="What changed" value={dim} onChange={e => setDim(e.target.value)}>{DIMS.map(d => <option key={d} value={d}>{humanize(d)}</option>)}</Select>
                <Select label="Baseline" value={a} onChange={e => setA(e.target.value)}>{(vals.data?.values || []).map(v => <option key={v.value} value={v.value}>{v.value} ({v.runs})</option>)}</Select>
                <Select label="Candidate" value={b} onChange={e => setB(e.target.value)}>{(vals.data?.values || []).map(v => <option key={v.value} value={v.value}>{v.value} ({v.runs})</option>)}</Select>
                <Button onClick={compare} disabled={!a || !b}>Compare</Button>
              </div>
              {vals.data && !vals.data.values.length && <p className="mt-2 text-sm text-muted">No runs record a {humanize(dim)} for {agent}.</p>}
              {cmpErr && <p className="mt-2 text-sm text-bad">{cmpErr}</p>}
              {cmp && <div className="mt-4"><ReportView r={cmp} /></div>}
            </Panel>
          </div>
        </div>
      )}
    </>
  );
}
