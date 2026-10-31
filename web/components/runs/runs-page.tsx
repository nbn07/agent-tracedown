"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { RotateCcw } from "lucide-react";
import { useApi } from "@/lib/api";
import type { RunSummary } from "@/lib/types";
import { ago, ms, num, pct, short } from "@/lib/format";
import { PageHeader } from "@/components/page-header";
import { Select } from "@/components/ui/select";
import { Badge, outcomeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Empty, ErrorState, Loading } from "@/components/ui/states";
import { VerdictBadge } from "@/components/verdict";

const PAGE = 50;

export function RunsPage() {
  const params = useSearchParams();
  const router = useRouter();
  const project = params.get("project") || "";
  const agent = params.get("agent") || "";
  const outcome = params.get("outcome") || "";
  const page = Math.max(0, Number(params.get("page") || 0));

  const set = (k: string, v: string) => {
    const p = new URLSearchParams(params.toString());
    v ? p.set(k, v) : p.delete(k);
    if (k !== "page") p.delete("page");
    router.replace(`/runs${p.toString() ? `?${p}` : ""}`);
  };

  const q = new URLSearchParams({ limit: String(PAGE), offset: String(page * PAGE) });
  if (project) q.set("project", project);
  if (agent) q.set("agent", agent);
  if (outcome) q.set("outcome", outcome);
  const { data, error, loading, reload } = useApi<{ runs: RunSummary[] }>(`/runs?${q}`);
  // filter choices: every project, and the agents in the chosen one
  const facets = useApi<{ projects: string[]; agents: string[] }>(`/facets${project ? `?project=${encodeURIComponent(project)}` : ""}`);
  const projects = facets.data?.projects || [];
  const agents = facets.data?.agents || [];

  const runs = data?.runs || [];
  return (
    <>
      <PageHeader title="Runs" sub="Every recorded agent run. Failed runs show the step that caused the failure and who was at fault."
        action={<Button variant="ghost" size="sm" onClick={reload}><RotateCcw size={14} />Refresh</Button>} />
      <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4 lg:max-w-3xl">
        <Select label="Project" value={project} onChange={e => set("project", e.target.value)}>
          <option value="">All projects</option>{projects.map(p => <option key={p}>{p}</option>)}
        </Select>
        <Select label="Agent" value={agent} onChange={e => set("agent", e.target.value)}>
          <option value="">All agents</option>{agents.map(a => <option key={a}>{a}</option>)}
        </Select>
        <Select label="Outcome" value={outcome} onChange={e => set("outcome", e.target.value)}>
          <option value="">Any outcome</option><option value="failure">Failed</option><option value="success">Succeeded</option>
        </Select>
      </div>
      {error ? <ErrorState error={error} onRetry={reload} /> : loading && !data ? <Loading rows={8} /> : !runs.length ? (
        <Empty title="No runs yet">Instrument an agent with the SDK, import runs with <code className="font-mono">tracedown-import</code>, or run <code className="font-mono">python examples/run_demo.py --endpoint …</code>.</Empty>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-line bg-panel">
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-muted">
              <tr className="border-b border-line">
                <th className="px-3 py-2 font-medium">Outcome</th>
                <th className="px-3 py-2 font-medium">Agent</th>
                <th className="px-3 py-2 font-medium">Root cause</th>
                <th className="px-3 py-2 font-medium">At fault</th>
                <th className="px-3 py-2 font-medium text-right">Unsupported claims</th>
                <th className="px-3 py-2 font-medium text-right">Steps</th>
                <th className="px-3 py-2 font-medium text-right">Tokens</th>
                <th className="px-3 py-2 font-medium text-right">Duration</th>
                <th className="px-3 py-2 font-medium text-right">Started</th>
              </tr>
            </thead>
            <tbody>
              {runs.map(r => (
                <tr key={r.trace_id} className="border-b border-line last:border-0 hover:bg-sunken/60">
                  <td className="px-3 py-2"><Badge tone={outcomeTone(r.outcome)}>{r.outcome}</Badge></td>
                  <td className="px-3 py-2 min-w-[180px]">
                    <Link href={`/runs/${r.trace_id}`} className="font-medium hover:underline underline-offset-2">{r.agent_name}</Link>
                    <div className="font-mono text-[11px] text-muted">{short(r.trace_id)} · {r.project}{r.replay_of && <> · replay of {short(r.replay_of)}</>}</div>
                  </td>
                  <td className="px-3 py-2 max-w-[360px]">
                    {r.root_cause ? (
                      <div className="truncate" title={r.root_cause.title}>
                        <span className="font-medium text-cause">{r.root_cause.label}</span>
                        {r.root_cause.step != null && <span className="text-muted"> · step {r.root_cause.step}</span>}
                      </div>
                    ) : <span className="text-muted">{r.outcome === "failure" ? "not analyzed" : "—"}</span>}
                  </td>
                  <td className="px-3 py-2">{r.outcome === "failure" ? <VerdictBadge fault={r.fault} /> : <span className="text-muted">—</span>}</td>
                  <td className="px-3 py-2 text-right tabular">{r.hallucination_score == null ? "—" : pct(r.hallucination_score)}</td>
                  <td className="px-3 py-2 text-right tabular">{r.span_count}</td>
                  <td className="px-3 py-2 text-right tabular">{num((r.input_tokens || 0) + (r.output_tokens || 0))}</td>
                  <td className="px-3 py-2 text-right tabular">{ms(r.duration_ms)}</td>
                  <td className="px-3 py-2 text-right text-muted whitespace-nowrap">{ago(r.started_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {(page > 0 || runs.length === PAGE) && (
        <div className="mt-3 flex items-center justify-end gap-2 text-sm">
          <Button size="sm" disabled={page === 0} onClick={() => set("page", String(page - 1))}>Previous</Button>
          <span className="text-muted">Page {page + 1}</span>
          <Button size="sm" disabled={runs.length < PAGE} onClick={() => set("page", String(page + 1))}>Next</Button>
        </div>
      )}
    </>
  );
}
