"use client";
import "@xyflow/react/dist/style.css";
import { useMemo } from "react";
import { Background, Controls, Handle, MiniMap, Position, ReactFlow, ReactFlowProvider, type Edge, type Node, type NodeProps } from "@xyflow/react";
import { FitBox } from "@/components/fit";
import type { RootCause, Span } from "@/lib/types";
import { kindOf, shortName, stepOf } from "@/lib/kinds";
import { ms } from "@/lib/format";

type StepData = { span: Span; isCause: boolean; isSymptom: boolean; selected: boolean };
type LaneData = { label: string; width: number; error: boolean };

const LANE_H = 112, COL_W = 186, X0 = 214;

function StepNode({ data }: NodeProps<Node<StepData>>) {
  const { span, isCause, isSymptom, selected } = data;
  const k = kindOf(span.kind);
  const err = span.status.code === "ERROR";
  const ring = isCause ? "var(--cause)" : isSymptom || err ? "var(--bad)" : selected ? "var(--accent)" : "var(--line)";
  return (
    <div className="relative w-[168px] rounded-md bg-panel px-2.5 py-2 text-left shadow-sm"
      style={{ border: `${isCause || selected ? 2 : 1}px solid ${ring}`, boxShadow: isCause ? "0 0 0 4px color-mix(in srgb, var(--cause) 18%, transparent)" : undefined }}>
      <Handle type="target" position={Position.Left} className="!h-1.5 !w-1.5 !border-0 !bg-line" />
      {isCause && <div className="absolute -top-2.5 left-2 rounded bg-cause px-1.5 text-[9px] font-bold uppercase tracking-wider text-white">root cause</div>}
      {!isCause && isSymptom && <div className="absolute -top-2.5 left-2 rounded bg-bad px-1.5 text-[9px] font-bold uppercase tracking-wider text-white">failed here</div>}
      <div className="flex items-center gap-1.5 text-[10px] text-muted">
        <k.icon size={11} style={{ color: k.color }} aria-hidden />
        <span className="uppercase tracking-wider">{k.label}</span>
        <span className="ml-auto font-mono tabular">#{stepOf(span)}</span>
      </div>
      <div className="mt-0.5 truncate text-[12.5px] font-medium" title={span.name}>{shortName(span.name)}</div>
      <div className="mt-0.5 flex items-center gap-1.5 text-[10px] text-muted">
        <span className="h-1.5 w-1.5 rounded-full" style={{ background: err ? "var(--bad)" : "var(--ok)" }} aria-hidden />
        <span className="tabular">{ms(((span.end_time || span.start_time) - span.start_time) / 1e6)}</span>
      </div>
      <Handle type="source" position={Position.Right} className="!h-1.5 !w-1.5 !border-0 !bg-line" />
    </div>
  );
}

function AgentNode({ data }: NodeProps<Node<StepData>>) {
  const { span, isCause, selected } = data;
  const err = span.status.code === "ERROR";
  const k = kindOf("agent");
  return (
    <div className="w-[176px] rounded-md px-3 py-2 text-left" style={{ background: "color-mix(in srgb, var(--k-agent) 14%, var(--panel))", border: `${selected || isCause ? 2 : 1}px solid ${isCause ? "var(--cause)" : selected ? "var(--accent)" : "color-mix(in srgb, var(--k-agent) 45%, transparent)"}` }}>
      <Handle type="target" position={Position.Top} id="t" className="!h-1.5 !w-1.5 !border-0 !bg-line" />
      <Handle type="target" position={Position.Left} className="!opacity-0" />
      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider" style={{ color: k.color }}>
        <k.icon size={11} aria-hidden />agent<span className="ml-auto font-mono normal-case text-muted">#{stepOf(span)}</span>
      </div>
      <div className="mt-0.5 truncate font-display text-[17px] font-semibold leading-tight">{shortName(span.name)}</div>
      <div className="text-[10px] text-muted">{err ? "ended with an error" : "completed"}</div>
      <Handle type="source" position={Position.Right} className="!h-1.5 !w-1.5 !border-0 !bg-line" />
      <Handle type="source" position={Position.Bottom} id="b" className="!h-1.5 !w-1.5 !border-0 !bg-line" />
    </div>
  );
}

function LaneNode({ data }: NodeProps<Node<LaneData>>) {
  return <div className="rounded-lg" style={{ width: data.width, height: LANE_H - 14, background: "color-mix(in srgb, var(--k-agent) 5%, transparent)", border: "1px dashed color-mix(in srgb, var(--k-agent) 22%, transparent)" }} />;
}

const nodeTypes = { step: StepNode, agent: AgentNode, lane: LaneNode };

export function StepGraph(props: Parameters<typeof Graph>[0]) {
  return <ReactFlowProvider><Graph {...props} /></ReactFlowProvider>;
}

function Graph({ spans, findings, dataflow, selected, onSelect }: {
  spans: Span[]; findings: RootCause[]; dataflow: Record<string, string[]>; selected: string | null; onSelect: (id: string) => void;
}) {
  const { nodes, edges, box } = useMemo(() => {
    const byId = Object.fromEntries(spans.map(s => [s.span_id, s]));
    const sorted = [...spans].sort((a, b) => stepOf(a) - stepOf(b));
    const cause = findings.find(f => f.root_cause)?.span_id ?? null;
    const symptoms = new Set(findings.filter(f => f.symptom).map(f => f.span_id));
    const owner = (s: Span): string => {
      if (s.kind === "agent") return s.span_id;
      let p = s.parent_span_id ? byId[s.parent_span_id] : undefined;
      while (p && p.kind !== "agent") p = p.parent_span_id ? byId[p.parent_span_id] : undefined;
      return p ? p.span_id : sorted[0].span_id;
    };
    const agents = sorted.filter(s => s.kind === "agent");
    const laneOf: Record<string, number> = {};
    agents.forEach((a, i) => (laneOf[a.span_id] = i));
    const steps = sorted.filter(s => s.kind !== "agent");
    const width = X0 + Math.max(1, steps.length) * COL_W + 20;

    const nodes: Node[] = [];
    agents.forEach((a, i) => {
      nodes.push({ id: `lane-${a.span_id}`, type: "lane", position: { x: -16, y: i * LANE_H - 12 }, data: { label: a.name, width, error: false },
        draggable: false, selectable: false, focusable: false, zIndex: -1 });
      nodes.push({ id: a.span_id, type: "agent", position: { x: 0, y: i * LANE_H + 6 },
        data: { span: a, isCause: cause === a.span_id, isSymptom: symptoms.has(a.span_id), selected: selected === a.span_id } });
    });
    steps.forEach((s, col) => {
      const lane = laneOf[owner(s)] ?? 0;
      nodes.push({ id: s.span_id, type: "step", position: { x: X0 + col * COL_W, y: lane * LANE_H + 4 },
        data: { span: s, isCause: cause === s.span_id, isSymptom: symptoms.has(s.span_id) || (s.status.code === "ERROR" && !cause), selected: selected === s.span_id } });
    });

    const edges: Edge[] = [];
    // handoffs between agents
    for (const a of agents) {
      const parentAgent = a.parent_span_id ? owner(byId[a.parent_span_id] ?? a) : null;
      if (parentAgent && parentAgent !== a.span_id && byId[parentAgent]) {
        edges.push({ id: `h-${a.span_id}`, source: parentAgent, target: a.span_id, sourceHandle: "b", targetHandle: "t", type: "smoothstep",
          label: "hands off", style: { strokeDasharray: "5 4", stroke: "var(--k-agent)" } });
      }
    }
    // each agent to its first step, then the run's steps in order
    for (const a of agents) {
      const first = steps.find(s => owner(s) === a.span_id);
      if (first) edges.push({ id: `a-${a.span_id}`, source: a.span_id, target: first.span_id, type: "smoothstep", style: { stroke: "var(--line)" } });
    }
    for (let i = 1; i < steps.length; i++) {
      edges.push({ id: `s-${i}`, source: steps[i - 1].span_id, target: steps[i].span_id, type: "smoothstep", style: { stroke: "var(--line)", strokeWidth: 1.5 } });
    }
    // data flow found by the root-cause engine (an output used later)
    for (const [down, ups] of Object.entries(dataflow || {})) {
      for (const up of ups) {
        if (!byId[up] || !byId[down]) continue;
        const hot = up === cause || down === cause;
        edges.push({ id: `d-${up}-${down}`, source: up, target: down, type: "smoothstep", animated: hot, label: "data",
          style: { stroke: hot ? "var(--cause)" : "var(--accent)", strokeWidth: 2 } });
      }
    }
    const box = { x: -16, y: -14, w: width + 4, h: Math.max(1, agents.length) * LANE_H };
    return { nodes, edges, box };
  }, [spans, findings, dataflow, selected]);

  const lanes = nodes.filter(n => n.type === "lane").length;
  const height = Math.min(560, Math.max(240, lanes * LANE_H + 60));
  const many = nodes.length > 14;
  return (
    <div className="rounded-lg border border-line bg-bg" style={{ height }} role="figure" aria-label="Step graph: agents as lanes, steps left to right, the root cause highlighted">
      <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes}
        minZoom={0.2} maxZoom={1.8} nodesDraggable={false} nodesConnectable={false} edgesFocusable={false}
        onNodeClick={(_, n) => n.type !== "lane" && onSelect(n.id)} proOptions={{ hideAttribution: true }}>
        <Background gap={20} size={1} color="var(--line)" />
        {many && <MiniMap pannable zoomable nodeColor={n => (n.type === "lane" ? "transparent" : (n.data as StepData).isCause ? "var(--cause)" : kindOf((n.data as StepData).span?.kind || "").color)} />}
        <Controls showInteractive={false} position="bottom-right" />
        <FitBox box={box} pad={10} />
      </ReactFlow>
    </div>
  );
}
