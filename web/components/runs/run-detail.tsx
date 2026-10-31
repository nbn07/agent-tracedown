"use client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useMemo, useState } from "react";
import { ArrowLeft, GitCompare, Sparkles } from "lucide-react";
import { api, useApi } from "@/lib/api";
import type { Claims, RunDetail } from "@/lib/types";
import { ms, num, runCost, short, when } from "@/lib/format";
import { stepOf } from "@/lib/kinds";
import { Badge, outcomeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Panel } from "@/components/ui/panel";
import { Tabs } from "@/components/ui/tabs";
import { ErrorState, Loading } from "@/components/ui/states";
import { StackTrace } from "./stack-trace";
import { FaultCard } from "./fault-card";
import { Findings } from "./findings";
import { StepGraph } from "./step-graph";
import { Waterfall } from "./waterfall";
import { SpanPanel } from "./span-panel";
import { ClaimsView } from "./claims";
import { ReplayPlanner } from "./replay";

export function RunDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { data, error, loading, reload } = useApi<RunDetail>(`/runs/${id}`);
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState("graph");
  const [analyzing, setAnalyzing] = useState(false);
  const claims = useApi<Claims>(tab === "claims" ? `/runs/${id}/claims` : null);
  const replays = useApi<{ runs: { trace_id: string; replay_of: string | null; outcome: string }[] }>(data ? `/runs?agent=${encodeURIComponent(data.run.agent_name)}&limit=200` : null);

  const report = data?.findings || null;
  const findings = report?.findings || [];
  const causeSpan = report?.root_cause?.span_id ?? null;
  const selectedSpan = useMemo(() => data?.spans.find(s => s.span_id === selected) || null, [data, selected]);
  const steps = useMemo(() => (data?.spans || []).map(stepOf).sort((a, b) => a - b), [data]);
  const myReplays = (replays.data?.runs || []).filter(r => r.replay_of === id);

  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (loading && !data) return <Loading rows={10} />;
  if (!data) return null;
  const run = data.run;

  const select = (spanId: string) => { setSelected(spanId); if (tab !== "graph" && tab !== "timeline") setTab("graph"); };
  const analyze = () => { setAnalyzing(true); api(`/runs/${id}/analyze`, { method: "POST" }).then(reload).finally(() => setAnalyzing(false)); };

  return (
    <div className="space-y-5">
      <div>
        <Link href="/runs" className="inline-flex items-center gap-1 text-sm text-muted hover:text-ink"><ArrowLeft size={14} />Runs</Link>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <h1 className="text-3xl font-semibold">{run.agent_name}</h1>
          <Badge tone={outcomeTone(run.outcome)}>{run.outcome}</Badge>
          {run.replay_of && <Badge tone="accent">replay</Badge>}
        </div>
        <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted">
          <span className="font-mono">{run.trace_id}</span>
          <span>{run.project}</span><span>{when(run.started_at)}</span>
          <span className="tabular">{run.span_count} steps · {ms(run.duration_ms)} · {num((run.input_tokens || 0) + (run.output_tokens || 0))} tokens</span>
          {run.cost_usd != null && <span className="tabular" title={run.cost_complete === false ? `${run.unpriced_calls} model call(s) have no known price and are not included` : "Cost of the model calls in this run"}>{runCost(run)}</span>}
          {run.models?.length > 0 && <span>{run.models.join(", ")}</span>}
        </div>
        <div className="mt-2 flex flex-wrap gap-2">
          {run.replay_of && <>
            <Link className="text-sm text-accent underline underline-offset-2" href={`/runs/${run.replay_of}`}>original run {short(run.replay_of)}</Link>
            <Link className="inline-flex items-center gap-1 text-sm text-accent underline underline-offset-2" href={`/diff?a=${run.replay_of}&b=${run.trace_id}`}><GitCompare size={13} />compare side by side</Link>
          </>}
          {myReplays.map(r => (
            <Link key={r.trace_id} className="inline-flex items-center gap-1 text-sm text-accent underline underline-offset-2" href={`/diff?a=${id}&b=${r.trace_id}`}>
              <GitCompare size={13} />compare with replay {short(r.trace_id)} ({r.outcome})
            </Link>
          ))}
        </div>
      </div>

      {report?.stack_trace && (report.outcome === "failure" || findings.length > 0) ? (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
          <Panel title="AI stack trace" eyebrow="What went wrong" bodyClass="p-3"><StackTrace text={report.stack_trace} /></Panel>
          <div className="space-y-4">
            {report.fault && <FaultCard fault={report.fault} />}
            {report.root_cause && (
              <div className="rounded-lg border border-cause/40 bg-cause-soft/40 p-4">
                <div className="eyebrow">Root cause</div>
                <p className="mt-1 font-semibold text-cause">{report.root_cause.label} · step {report.root_cause.step}</p>
                <p className="mt-1 text-sm">{report.root_cause.title}</p>
                <button className="mt-2 text-sm text-accent underline underline-offset-2 cursor-pointer" onClick={() => select(report.root_cause!.span_id)}>Show in graph</button>
              </div>
            )}
          </div>
        </div>
      ) : run.outcome === "success" && !report ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-line bg-panel px-4 py-3 text-sm">
          <span>This run succeeded. Successful runs are analyzed on demand.</span>
          <Button size="sm" onClick={analyze} disabled={analyzing}><Sparkles size={13} />{analyzing ? "Analyzing…" : "Analyze anyway"}</Button>
        </div>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_380px]">
        <Panel bodyClass="p-3 pt-1">
          <Tabs value={tab} onValueChange={setTab} tabs={[
            { id: "graph", label: "Step graph", content: <StepGraph spans={data.spans} findings={findings} dataflow={report?.dataflow || {}} selected={selected} onSelect={setSelected} /> },
            { id: "timeline", label: "Timeline", content: <Waterfall spans={data.spans} selected={selected} onSelect={setSelected} cause={causeSpan} /> },
            { id: "findings", label: `Findings (${findings.length})`, content: <Findings findings={findings} onStep={select} /> },
            { id: "claims", label: "Claims", content: claims.error ? <ErrorState error={claims.error} /> : claims.data ? <ClaimsView data={claims.data} onStep={select} /> : <Loading rows={3} /> },
            { id: "replay", label: "Replay", content: <ReplayPlanner traceId={id} defaultStep={report?.root_cause?.step ?? null} steps={steps} /> },
          ]} />
        </Panel>
        <div>
          {selectedSpan ? <SpanPanel span={selectedSpan} findings={findings} onClose={() => setSelected(null)} /> : (
            <div className="rounded-lg border border-dashed border-line p-5 text-sm text-muted">
              Select a step in the graph or timeline to see its input, output and attributes.
              {causeSpan && <button className="mt-2 block text-accent underline underline-offset-2 cursor-pointer" onClick={() => setSelected(causeSpan)}>Open the root-cause step</button>}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
