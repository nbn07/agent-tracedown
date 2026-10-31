"use client";
import "@xyflow/react/dist/style.css";
import { useMemo } from "react";
import { Background, Controls, Handle, MarkerType, Position, ReactFlow, ReactFlowProvider, type Edge, type Node, type NodeProps } from "@xyflow/react";
import { FitBox } from "@/components/fit";
import type { AgentGraph, GraphNode } from "@/lib/types";
import { kindOf } from "@/lib/kinds";
import { pct } from "@/lib/format";

type D = { n: GraphNode; rate: number };

function NetNode({ data }: NodeProps<Node<D>>) {
  const { n, rate } = data;
  const k = kindOf(n.type);
  const hot = rate >= 0.2;
  return (
    <div className="w-[190px] rounded-lg bg-panel px-3 py-2 shadow-sm"
      style={{ border: `${hot ? 2 : 1}px solid ${hot ? "var(--bad)" : rate > 0 ? "var(--warn)" : "var(--line)"}`, borderLeft: `4px solid ${k.color}` }}>
      <Handle type="target" position={Position.Left} className="!h-1.5 !w-1.5 !border-0 !bg-line" />
      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider" style={{ color: k.color }}>
        <k.icon size={11} aria-hidden />{k.label}
      </div>
      <div className={`truncate font-medium ${n.type === "agent" ? "font-display text-[17px] leading-tight" : "text-[13px]"}`} title={n.label}>{n.label}</div>
      <div className="mt-1 flex items-center gap-2 text-[11px] text-muted tabular">
        <span>{n.calls} calls</span>
        <span className={hot ? "font-semibold text-bad" : rate > 0 ? "text-warn" : ""}>{pct(rate)} errors</span>
      </div>
      <div className="mt-1 h-1 overflow-hidden rounded bg-sunken" aria-hidden>
        <div className="h-full" style={{ width: `${Math.min(100, rate * 100)}%`, background: hot ? "var(--bad)" : "var(--warn)" }} />
      </div>
      <Handle type="source" position={Position.Right} className="!h-1.5 !w-1.5 !border-0 !bg-line" />
    </div>
  );
}
const nodeTypes = { net: NetNode };

export function AgentNetwork(props: { graph: AgentGraph; onNode?: (n: GraphNode) => void }) {
  return <ReactFlowProvider><Network {...props} /></ReactFlowProvider>;
}

function Network({ graph, onNode }: { graph: AgentGraph; onNode?: (n: GraphNode) => void }) {
  const { nodes, edges, box } = useMemo(() => {
    // layered layout: breadth-first from nodes nobody calls
    const incoming = new Map<string, number>();
    graph.edges.forEach(e => incoming.set(e.target, (incoming.get(e.target) || 0) + 1));
    const roots = graph.nodes.filter(n => !incoming.get(n.id)).map(n => n.id);
    const layer: Record<string, number> = {};
    const queue = roots.length ? [...roots] : graph.nodes.slice(0, 1).map(n => n.id);
    queue.forEach(r => (layer[r] = 0));
    while (queue.length) {
      const cur = queue.shift()!;
      for (const e of graph.edges.filter(e => e.source === cur)) {
        if (layer[e.target] == null) { layer[e.target] = layer[cur] + 1; queue.push(e.target); }
      }
    }
    graph.nodes.forEach(n => (layer[n.id] ??= 0));
    const rows: Record<number, number> = {};
    const nodes: Node[] = graph.nodes.map(n => {
      const L = layer[n.id];
      const r = (rows[L] = (rows[L] ?? -1) + 1);
      const rate = n.error_rate ?? (n.calls ? n.errors / n.calls : 0);
      return { id: n.id, type: "net", position: { x: L * 270, y: r * 112 }, data: { n, rate } };
    });
    const maxCalls = Math.max(1, ...graph.edges.map(e => e.calls));
    const edges: Edge[] = graph.edges.map(e => {
      const err = e.errors > 0;
      const handoff = e.source.startsWith("agent:") && e.target.startsWith("agent:");
      return {
        id: `${e.source}->${e.target}`, source: e.source, target: e.target, type: "smoothstep",
        label: `${e.calls}${e.errors ? ` · ${e.errors} err` : ""}`,
        markerEnd: { type: MarkerType.ArrowClosed, color: err ? "var(--bad)" : "var(--muted)" },
        style: { stroke: err ? "var(--bad)" : handoff ? "var(--k-agent)" : "var(--muted)", strokeWidth: 1 + 3 * (e.calls / maxCalls), strokeDasharray: handoff ? "6 4" : undefined },
      };
    });
    const maxX = Math.max(0, ...nodes.map(n => n.position.x)), maxY = Math.max(0, ...nodes.map(n => n.position.y));
    const box = { x: 0, y: 0, w: maxX + 190, h: maxY + 84 };
    return { nodes, edges, box };
  }, [graph]);

  return (
    <div className="h-[600px] rounded-lg border border-line bg-bg" role="figure" aria-label="Agent network: who calls whom, with call counts and error rates">
      <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes}
        minZoom={0.2} nodesConnectable={false} onNodeClick={(_, n) => onNode?.((n.data as D).n)} proOptions={{ hideAttribution: true }}>
        <Background gap={20} size={1} color="var(--line)" />
        <Controls showInteractive={false} position="bottom-right" />
        <FitBox box={box} pad={24} />
      </ReactFlow>
    </div>
  );
}
