"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useMemo } from "react";
import { Bar, BarChart, CartesianGrid, ComposedChart, Legend, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useApi } from "@/lib/api";
import type { Analytics, RunSummary } from "@/lib/types";
import { humanize, ms, num, pct, usd } from "@/lib/format";
import { PageHeader } from "@/components/page-header";
import { Panel, Stat } from "@/components/ui/panel";
import { Select } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { ErrorState, Loading } from "@/components/ui/states";

const axis = { tick: { fill: "var(--muted)", fontSize: 11 }, stroke: "var(--line)" };
const SERIES = ["var(--k-llm)", "var(--k-tool)", "var(--k-agent)", "var(--k-retrieval)", "var(--k-decision)", "var(--k-memory)"];
const tip = { contentStyle: { background: "var(--panel)", border: "1px solid var(--line)", color: "var(--ink)", fontSize: 12 }, cursor: { fill: "var(--sunken)" } };

export function AnalyticsPage() {
  const params = useSearchParams();
  const router = useRouter();
  const project = params.get("project") || "", agent = params.get("agent") || "", days = params.get("days") || "7";
  const set = (k: string, v: string) => { const p = new URLSearchParams(params.toString()); v ? p.set(k, v) : p.delete(k); router.replace(`/analytics?${p}`); };
  const q = new URLSearchParams({ days }); if (project) q.set("project", project); if (agent) q.set("agent", agent);
  const { data: a, error, loading, reload } = useApi<Analytics>(`/analytics?${q}`);
  const facets = useApi<{ projects: string[]; agents: string[] }>(`/facets${project ? `?project=${encodeURIComponent(project)}` : ""}`);
  const projects = facets.data?.projects || [];
  const agents = facets.data?.agents || [];

  return (
    <>
      <PageHeader title="Analytics" sub="How reliable, fast and expensive your agents are, and which tools and sources let them down." />
      <div className="mb-5 grid grid-cols-3 gap-3 lg:max-w-2xl">
        <Select label="Project" value={project} onChange={e => set("project", e.target.value)}><option value="">All</option>{projects.map(p => <option key={p}>{p}</option>)}</Select>
        <Select label="Agent" value={agent} onChange={e => set("agent", e.target.value)}><option value="">All</option>{agents.map(x => <option key={x}>{x}</option>)}</Select>
        <Select label="Window" value={days} onChange={e => set("days", e.target.value)}>{["1", "7", "30", "90"].map(d => <option key={d} value={d}>Last {d} day{d === "1" ? "" : "s"}</option>)}</Select>
      </div>
      {error ? <ErrorState error={error} onRetry={reload} /> : !a ? <Loading rows={8} /> : (() => {
        const o = a.overall;
        const daily = a.daily.map(d => ({ ...d, successes: d.runs - d.failures }));
        const tools = [...a.tools].sort((x, y) => y.error_rate - x.error_rate || y.calls - x.calls).slice(0, 12);
        const models = a.models || [];
        const maxModelCost = Math.max(0, ...models.map(m => m.cost_usd || 0));
        // the five most expensive agents get their own series; the rest are summed as "other"
        const agentTotals = new Map<string, number>();
        a.daily.forEach(d => Object.entries(d.cost_by_agent || {}).forEach(([ag, v]) => agentTotals.set(ag, (agentTotals.get(ag) || 0) + v)));
        const ranked = [...agentTotals.entries()].filter(([, v]) => v > 0).sort((x, y) => y[1] - x[1]).map(([ag]) => ag);
        const top = ranked.slice(0, 5);
        const costAgents = ranked.length > 5 ? [...top, "other"] : top;
        const costDaily = a.daily.map(d => {
          const row: Record<string, number | string> = { day: d.day };
          for (const ag of costAgents) row[ag] = 0;
          Object.entries(d.cost_by_agent || {}).forEach(([ag, v]) => { const k = top.includes(ag) ? ag : "other"; if (k in row) row[k] = Number(row[k]) + v; });
          return row;
        });
        return (
          <div className={`space-y-4 ${loading ? "opacity-70" : ""}`}>
            <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
              <Stat label="Runs" value={num(o.runs)} sub={`${num(o.failures)} failed`} />
              <Stat label="Success rate" value={pct(o.success_rate)} tone={o.success_rate == null ? undefined : o.success_rate >= 0.9 ? "ok" : o.success_rate >= 0.7 ? "warn" : "bad"} sub={`${num(o.steps_per_run)} steps per run`} />
              <Stat label="Latency p50" value={ms(o.latency_ms.p50)} sub={`p95 ${ms(o.latency_ms.p95)}`} />
              <Stat label="Cost per task" value={usd(o.cost_usd.per_task)} sub={`${usd(o.cost_usd.total)} total${o.cost_usd.runs_without_price ? ` · ${o.cost_usd.runs_without_price} unpriced` : ""}`} />
              <Stat label="Tokens per run" value={num(o.tokens.per_run)} sub={`${num(o.tokens.total)} total`} />
              <Stat label="Unsupported claims" value={pct(o.hallucination_rate)} tone={o.hallucination_rate && o.hallucination_rate > 0.2 ? "bad" : undefined} sub={o.feedback_score != null ? `user feedback ${pct(o.feedback_score)}` : "answers with a claim no source backs"} />
            </div>
            {o.cost_usd.runs_without_price > 0 && <p className="text-xs text-muted">{o.cost_usd.runs_without_price} run{o.cost_usd.runs_without_price === 1 ? "" : "s"} used a model with no known price, so {o.cost_usd.runs_without_price === 1 ? "its" : "their"} cost is left out. Add the price with <code className="font-mono">TDN_PRICES</code> or PUT <code className="font-mono">/api/v1/settings/prices</code>.</p>}

            <div className="grid gap-4 xl:grid-cols-2">
              <Panel title="Runs per day">
                <div className="h-64">
                  <ResponsiveContainer width="100%" height="100%">
                    <ComposedChart data={daily} margin={{ left: -16, right: 8, top: 4 }}>
                      <CartesianGrid stroke="var(--line)" vertical={false} />
                      <XAxis dataKey="day" {...axis} />
                      <YAxis yAxisId="r" allowDecimals={false} {...axis} />
                      <YAxis yAxisId="c" orientation="right" {...axis} tickFormatter={v => `$${Number(v).toFixed(3)}`} width={60} />
                      <Tooltip {...tip} />
                      <Legend wrapperStyle={{ fontSize: 12, color: "var(--muted)" }} />
                      <Bar yAxisId="r" dataKey="successes" name="succeeded" stackId="a" fill="var(--ok)" radius={[0, 0, 0, 0]} />
                      <Bar yAxisId="r" dataKey="failures" name="failed" stackId="a" fill="var(--bad)" radius={[3, 3, 0, 0]} />
                      <Line yAxisId="c" dataKey="cost_usd" name="cost (USD)" stroke="var(--accent)" strokeWidth={2} dot={{ r: 3 }} />
                    </ComposedChart>
                  </ResponsiveContainer>
                </div>
              </Panel>
              <Panel title="Tool reliability" eyebrow="Error rate by tool">
                <div className="h-64">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={tools} layout="vertical" margin={{ left: 8, right: 16 }}>
                      <CartesianGrid stroke="var(--line)" horizontal={false} />
                      <XAxis type="number" domain={[0, 1]} tickFormatter={v => `${Math.round(v * 100)}%`} {...axis} />
                      <YAxis type="category" dataKey="tool" width={150} {...axis} />
                      <Tooltip {...tip} formatter={(v) => pct(Number(v))} />
                      <Bar dataKey="error_rate" name="error rate" fill="var(--bad)" radius={[0, 3, 3, 0]} background={{ fill: "var(--sunken)" }} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </Panel>
            </div>

            <div className="grid gap-4 xl:grid-cols-2">
              <Panel title="Cost per day" eyebrow="By agent, USD">
                <div className="h-64">
                  {costAgents.length ? (
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={costDaily} margin={{ left: -4, right: 8, top: 4 }}>
                        <CartesianGrid stroke="var(--line)" vertical={false} />
                        <XAxis dataKey="day" {...axis} />
                        <YAxis {...axis} tickFormatter={v => usd(Number(v))} width={64} />
                        <Tooltip {...tip} formatter={(v) => usd(Number(v))} />
                        <Legend wrapperStyle={{ fontSize: 12, color: "var(--muted)" }} />
                        {costAgents.map((ag, i) => (
                          <Bar key={ag} dataKey={ag} name={ag} stackId="c" fill={SERIES[i % SERIES.length]}
                            radius={i === costAgents.length - 1 ? [3, 3, 0, 0] : [0, 0, 0, 0]} />
                        ))}
                      </BarChart>
                    </ResponsiveContainer>
                  ) : <p className="text-sm text-muted">No priced model calls in this window.</p>}
                </div>
              </Panel>
              <Panel title="Cost by model" bodyClass="p-0">
                {models.length ? (
                  <table className="w-full text-sm"><thead className="text-left text-xs text-muted"><tr className="border-b border-line"><th className="px-4 py-2 font-medium">Model</th><th className="px-3 py-2 text-right font-medium">Calls</th><th className="px-3 py-2 text-right font-medium">Tokens in / out</th><th className="px-3 py-2 font-medium">Cost</th></tr></thead>
                    <tbody>{models.map(m => (
                      <tr key={m.model} className="border-b border-line last:border-0">
                        <td className="px-4 py-1.5 font-mono text-xs">{m.model}</td>
                        <td className="px-3 py-1.5 text-right tabular">{m.calls}</td>
                        <td className="px-3 py-1.5 text-right tabular whitespace-nowrap">{num(m.input_tokens)} / {num(m.output_tokens)}</td>
                        <td className="px-3 py-1.5">{m.cost_usd == null ? <span className="text-muted">no price</span> : (
                          <div className="flex items-center gap-2"><div className="h-2 w-24 overflow-hidden rounded bg-sunken"><div className="h-full bg-accent" style={{ width: `${maxModelCost ? (m.cost_usd / maxModelCost) * 100 : 0}%` }} /></div><span className="tabular">{usd(m.cost_usd)}{m.unpriced_calls ? "+" : ""}</span></div>
                        )}</td>
                      </tr>
                    ))}</tbody></table>
                ) : <p className="p-4 text-sm text-muted">No model calls in this window.</p>}
              </Panel>
            </div>

            <Panel title="Agents" bodyClass="p-0">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="text-left text-xs text-muted"><tr className="border-b border-line">
                    <th className="px-4 py-2 font-medium">Agent</th><th className="px-3 py-2 text-right font-medium">Runs</th><th className="px-3 py-2 font-medium">Success rate</th>
                    <th className="px-3 py-2 text-right font-medium">p50 / p95</th><th className="px-3 py-2 text-right font-medium">Cost per task</th><th className="px-3 py-2 font-medium">Top causes of failure</th></tr></thead>
                  <tbody>{Object.entries(a.agents).sort((x, y) => y[1].runs - x[1].runs).map(([name, s]) => (
                    <tr key={name} className="border-b border-line last:border-0">
                      <td className="px-4 py-2"><Link href={`/network?agent=${encodeURIComponent(name)}`} className="font-medium hover:underline underline-offset-2">{name}</Link></td>
                      <td className="px-3 py-2 text-right tabular">{s.runs}</td>
                      <td className="px-3 py-2"><div className="flex items-center gap-2"><div className="h-2 w-24 overflow-hidden rounded bg-sunken"><div className="h-full" style={{ width: `${(s.success_rate ?? 0) * 100}%`, background: (s.success_rate ?? 0) >= 0.9 ? "var(--ok)" : (s.success_rate ?? 0) >= 0.7 ? "var(--warn)" : "var(--bad)" }} /></div><span className="tabular">{pct(s.success_rate)}</span></div></td>
                      <td className="px-3 py-2 text-right tabular whitespace-nowrap">{ms(s.latency_ms.p50)} / {ms(s.latency_ms.p95)}</td>
                      <td className="px-3 py-2 text-right tabular">{usd(s.cost_usd.per_task)}</td>
                      <td className="px-3 py-2"><div className="flex flex-wrap gap-1">{s.top_root_causes.slice(0, 3).map(([c, n]) => <Badge key={c} tone="cause">{humanize(c)} · {n}</Badge>)}</div></td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            </Panel>

            <div className="grid gap-4 xl:grid-cols-2">
              <Panel title="Tools" bodyClass="p-0">
                <table className="w-full text-sm"><thead className="text-left text-xs text-muted"><tr className="border-b border-line"><th className="px-4 py-2 font-medium">Tool</th><th className="px-3 py-2 text-right font-medium">Calls</th><th className="px-3 py-2 text-right font-medium">Errors</th><th className="px-3 py-2 text-right font-medium">p95</th></tr></thead>
                  <tbody>{a.tools.map(t => <tr key={t.tool} className="border-b border-line last:border-0"><td className="px-4 py-1.5">{t.tool}</td><td className="px-3 py-1.5 text-right tabular">{t.calls}</td><td className={`px-3 py-1.5 text-right tabular ${t.errors ? "text-bad" : ""}`}>{t.errors} ({pct(t.error_rate)})</td><td className="px-3 py-1.5 text-right tabular">{ms(t.latency_ms.p95)}</td></tr>)}</tbody></table>
              </Panel>
              <Panel title="Retrieval sources" bodyClass="p-0">
                {a.retrieval.length ? (
                  <table className="w-full text-sm"><thead className="text-left text-xs text-muted"><tr className="border-b border-line"><th className="px-4 py-2 font-medium">Source</th><th className="px-3 py-2 text-right font-medium">Queries</th><th className="px-3 py-2 text-right font-medium">Empty</th><th className="px-3 py-2 text-right font-medium">Stale</th><th className="px-3 py-2 text-right font-medium">Used in answer</th></tr></thead>
                    <tbody>{a.retrieval.map(r => <tr key={r.source} className="border-b border-line last:border-0"><td className="px-4 py-1.5">{r.source}</td><td className="px-3 py-1.5 text-right tabular">{r.queries}</td><td className="px-3 py-1.5 text-right tabular">{pct(r.empty_rate)}</td><td className={`px-3 py-1.5 text-right tabular ${r.stale_rate > 0 ? "text-warn" : ""}`}>{pct(r.stale_rate)}</td><td className="px-3 py-1.5 text-right tabular">{pct(r.reached_answer_rate)}</td></tr>)}</tbody></table>
                ) : <p className="p-4 text-sm text-muted">No retrieval steps in this window.</p>}
              </Panel>
            </div>
          </div>
        );
      })()}
    </>
  );
}
