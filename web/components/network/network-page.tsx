"use client";
import { useRouter, useSearchParams } from "next/navigation";
import { useMemo, useState } from "react";
import { useApi } from "@/lib/api";
import type { AgentGraph, GraphNode, RunSummary } from "@/lib/types";
import { PageHeader } from "@/components/page-header";
import { Select } from "@/components/ui/select";
import { Empty, ErrorState, Loading } from "@/components/ui/states";
import { Panel } from "@/components/ui/panel";
import { kindOf } from "@/lib/kinds";
import { pct } from "@/lib/format";
import { AgentNetwork } from "./agent-graph";

export function NetworkPage() {
  const params = useSearchParams();
  const router = useRouter();
  const runs = useApi<{ runs: RunSummary[] }>("/runs?limit=500");
  const agents = useMemo(() => {
    const c = new Map<string, number>();
    (runs.data?.runs || []).forEach(r => c.set(r.agent_name, (c.get(r.agent_name) || 0) + 1));
    return [...c.entries()].sort((a, b) => b[1] - a[1]).map(([a]) => a);
  }, [runs.data]);
  const agent = params.get("agent") || agents[0] || "";
  const graph = useApi<AgentGraph>(agent ? `/agents/${encodeURIComponent(agent)}/graph?limit=500` : null);
  const [picked, setPicked] = useState<GraphNode | null>(null);

  return (
    <>
      <PageHeader title="Agent network" sub="Who calls whom across recent runs. Line width shows traffic; red shows errors; dashed lines are handoffs between agents." />
      <div className="mb-4 max-w-xs">
        <Select label="Top-level agent" value={agent} onChange={e => { setPicked(null); router.replace(`/network?agent=${encodeURIComponent(e.target.value)}`); }}>
          {agents.map(a => <option key={a}>{a}</option>)}
        </Select>
      </div>
      {runs.error ? <ErrorState error={runs.error} onRetry={runs.reload} /> : !agent && !runs.loading ? <Empty title="No runs yet">The network appears once agents have run.</Empty> :
        graph.error ? <ErrorState error={graph.error} onRetry={graph.reload} /> : !graph.data ? <Loading rows={6} /> : (
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_300px]">
            <div>
              <AgentNetwork graph={graph.data} onNode={setPicked} />
              <p className="mt-2 text-xs text-muted">{graph.data.runs ?? 0} runs · {graph.data.nodes.length} components · drag to rearrange, scroll to zoom</p>
            </div>
            <Panel title={picked ? picked.label : "Busiest and most failing"} eyebrow={picked ? kindOf(picked.type).label : "Components"}>
              {picked ? (
                <dl className="grid grid-cols-2 gap-y-2 text-sm">
                  <dt className="text-muted">Calls</dt><dd className="tabular text-right">{picked.calls}</dd>
                  <dt className="text-muted">Errors</dt><dd className="tabular text-right">{picked.errors}</dd>
                  <dt className="text-muted">Error rate</dt><dd className="tabular text-right">{pct(picked.error_rate ?? (picked.calls ? picked.errors / picked.calls : 0))}</dd>
                  {picked.runs != null && <><dt className="text-muted">Runs that used it</dt><dd className="tabular text-right">{picked.runs}</dd></>}
                </dl>
              ) : (
                <ul className="space-y-1.5 text-sm">
                  {[...graph.data.nodes].sort((a, b) => (b.errors - a.errors) || (b.calls - a.calls)).slice(0, 10).map(n => (
                    <li key={n.id}><button className="flex w-full items-center justify-between gap-2 text-left hover:underline cursor-pointer" onClick={() => setPicked(n)}>
                      <span className="truncate">{n.label}</span>
                      <span className={`tabular text-xs ${n.errors ? "text-bad" : "text-muted"}`}>{n.calls} · {n.errors} err</span>
                    </button></li>
                  ))}
                </ul>
              )}
            </Panel>
          </div>
        )}
    </>
  );
}
