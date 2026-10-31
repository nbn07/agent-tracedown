"use client";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { ArrowLeft } from "lucide-react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api, useApi } from "@/lib/api";
import type { Incident } from "@/lib/types";
import { ago, humanize, short, when } from "@/lib/format";
import { PageHeader } from "@/components/page-header";
import { Badge, severityTone, type Tone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Panel } from "@/components/ui/panel";
import { Select } from "@/components/ui/select";
import { Empty, ErrorState, Loading } from "@/components/ui/states";

const statusTone = (s: string): Tone => (s === "resolved" ? "ok" : s === "acknowledged" ? "accent" : "warn");

export function IncidentsPage() {
  const params = useSearchParams();
  const router = useRouter();
  const status = params.get("status") || "";
  const { data, error, loading, reload } = useApi<{ incidents: Incident[] }>(`/incidents${status ? `?status=${status}` : ""}`);
  const list = data?.incidents || [];
  return (
    <>
      <PageHeader title="Incidents" sub="Failed runs grouped by root cause. One incident per recurring problem, with severity rising as it repeats." />
      <div className="mb-4 max-w-[200px]">
        <Select label="Status" value={status} onChange={e => router.replace(`/incidents${e.target.value ? `?status=${e.target.value}` : ""}`)}>
          <option value="">Any status</option><option value="open">Open</option><option value="acknowledged">Acknowledged</option><option value="resolved">Resolved</option>
        </Select>
      </div>
      {error ? <ErrorState error={error} onRetry={reload} /> : loading && !data ? <Loading rows={6} /> : !list.length ? <Empty title="No incidents">Incidents open automatically when failed runs share a root cause.</Empty> : (
        <ul className="space-y-2">
          {list.map(i => (
            <li key={i.id}>
              <Link href={`/incidents/${i.id}`} className="flex items-stretch gap-3 rounded-lg border border-line bg-panel px-4 py-3 hover:border-ink/30">
                <span className="w-1 self-stretch rounded" style={{ background: i.severity === "high" || i.severity === "critical" ? "var(--bad)" : i.severity === "medium" ? "var(--warn)" : "var(--line)" }} aria-hidden />
                <div className="flex min-w-0 flex-1 flex-col gap-2 sm:flex-row sm:items-center sm:gap-3"><div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-xs text-muted">#{i.id}</span>
                    <span className="font-semibold">{i.title}</span>
                  </div>
                  <p className="mt-0.5 truncate text-sm text-muted">{i.root_cause}</p>
                </div>
                <div className="flex shrink-0 flex-wrap items-center gap-2 text-xs">
                  <Badge tone={severityTone(i.severity)}>{i.severity}</Badge>
                  <Badge tone={statusTone(i.status)}>{i.status}</Badge>
                  <span className="tabular text-muted sm:w-16 sm:text-right">{i.run_count} run{i.run_count === 1 ? "" : "s"}</span>
                  <span className="text-muted sm:w-20 sm:text-right">{ago(i.last_seen)}</span>
                </div></div>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

export function IncidentDetail() {
  const { id } = useParams<{ id: string }>();
  const { data: i, error, loading, reload } = useApi<Incident>(`/incidents/${id}`);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (loading && !i) return <Loading rows={8} />;
  if (!i) return null;
  const setStatus = (s: string) => { setBusy(true); api(`/incidents/${id}/status`, { method: "POST", body: JSON.stringify({ status: s }) }).then(() => { setErr(null); reload(); }).catch(e => setErr(e.message)).finally(() => setBusy(false)); };
  const hours = (i.timeline?.runs_per_hour || []).map(h => ({ hour: new Date(h.hour_unix * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit" }), runs: h.runs }));
  return (
    <div className="space-y-5">
      <Link href="/incidents" className="inline-flex items-center gap-1 text-sm text-muted hover:text-ink"><ArrowLeft size={14} />Incidents</Link>
      <PageHeader eyebrow={`Incident #${i.id} · ${i.project}`} title={i.title}
        sub={<span className="flex flex-wrap items-center gap-2"><Badge tone={severityTone(i.severity)}>{i.severity}</Badge><Badge tone={statusTone(i.status)}>{i.status}</Badge>
          <span>{i.run_count} runs · first {when(i.first_seen)} · last {ago(i.last_seen)}</span></span>}
        action={<div className="flex gap-2">
          {i.status !== "acknowledged" && i.status !== "resolved" && <Button disabled={busy} onClick={() => setStatus("acknowledged")}>Acknowledge</Button>}
          {i.status !== "resolved" ? <Button variant="primary" disabled={busy} onClick={() => setStatus("resolved")}>Resolve</Button> : <Button disabled={busy} onClick={() => setStatus("open")}>Reopen</Button>}
        </div>} />
      {err && <p className="text-sm text-bad">{err}</p>}
      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Root cause" eyebrow={humanize(i.category)}>
          <p className="text-sm">{i.root_cause}</p>
          <div className="mt-3 rounded-md bg-sunken p-3 text-sm"><div className="eyebrow mb-1">Suggested fix</div>{i.suggested_fix}</div>
          <p className="mt-3 break-words font-mono text-[11px] text-muted">signature: {i.signature}</p>
        </Panel>
        <Panel title="Runs per hour">
          {hours.length ? (
            <div className="h-48">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={hours} margin={{ left: -20, right: 8, top: 4 }}>
                  <CartesianGrid stroke="var(--line)" vertical={false} />
                  <XAxis dataKey="hour" tick={{ fill: "var(--muted)", fontSize: 11 }} stroke="var(--line)" />
                  <YAxis allowDecimals={false} tick={{ fill: "var(--muted)", fontSize: 11 }} stroke="var(--line)" />
                  <Tooltip contentStyle={{ background: "var(--panel)", border: "1px solid var(--line)", color: "var(--ink)" }} cursor={{ fill: "var(--sunken)" }} />
                  <Bar dataKey="runs" fill="var(--cause)" radius={[3, 3, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : <p className="text-sm text-muted">No hourly data.</p>}
        </Panel>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Timeline">
          <ol className="relative space-y-3 border-l border-line pl-4">
            {(i.timeline?.events || []).map((e, k) => (
              <li key={k} className="relative">
                <span className="absolute -left-[21px] top-1.5 h-2.5 w-2.5 rounded-full border-2 border-panel" style={{ background: e.kind === "resolved" ? "var(--ok)" : e.kind === "opened" || e.kind === "regressed" ? "var(--bad)" : "var(--warn)" }} aria-hidden />
                <div className="text-sm font-medium">{humanize(e.kind)}</div>
                <div className="text-xs text-muted">{when(e.at)}{Object.keys(e.detail || {}).length ? ` · ${Object.entries(e.detail).map(([k2, v]) => `${humanize(k2)} ${typeof v === "string" && v.length > 20 ? short(v) : v}`).join(", ")}` : ""}</div>
              </li>
            ))}
          </ol>
        </Panel>
        <Panel title={`Affected runs (${i.affected_runs?.length || 0})`}>
          <ul className="flex flex-wrap gap-2">
            {(i.affected_runs || []).map(t => <li key={t}><Link className="rounded border border-line px-2 py-1 font-mono text-xs hover:border-ink/40" href={`/runs/${t}`}>{short(t)}</Link></li>)}
          </ul>
        </Panel>
      </div>
    </div>
  );
}
