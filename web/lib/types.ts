// Shapes returned by the Tracedown API (server/tracedown_server/api).

export type Outcome = "success" | "failure" | string;

export interface RootCause {
  id: string;
  category: string;
  label: string;
  title: string;
  span_id: string;
  step: number | null;
  confidence: number;
  detector: string;
  evidence: string[];
  symptom: boolean;
  caused_by: string | null;
  score?: number;
  root_cause?: boolean;
}

export interface RunSummary {
  trace_id: string;
  project: string;
  agent_name: string;
  outcome: Outcome;
  error: string | null;
  started_at: number; // unix ns
  duration_ms: number;
  span_count: number;
  input_tokens: number;
  output_tokens: number;
  models: string[];
  replay_of: string | null;
  root_cause: RootCause | null;
  hallucination_score: number | null;
  fault?: Fault | null;
  /** USD; null when no model call in the run has a known price */
  cost_usd?: number | null;
  /** false when some model calls had no known price (cost_usd then covers the priced ones) */
  cost_complete?: boolean;
  unpriced_calls?: number;
}

export interface Span {
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  name: string;
  kind: string;
  start_time: number;
  end_time: number | null;
  status: { code: string; message: string };
  attributes: Record<string, unknown>;
  input_digest: string | null;
  output_digest: string | null;
  /** model calls only: USD, and whether the caller reported it or it was computed from tokens */
  cost_usd?: number | null;
  cost_source?: "reported" | "computed" | null;
}

export interface Fault {
  verdict: "agent" | "user" | "unfinished" | "unclear";
  confidence: number;
  step: number | null;
  reason: string;
  hints?: string[];
  source?: string;
}

export interface Report {
  trace_id: string;
  agent: string;
  outcome: Outcome;
  findings: RootCause[];
  root_cause: RootCause | null;
  dataflow: Record<string, string[]>;
  fault?: Fault | null;
  stack_trace: string;
}

export interface RunDetail {
  run: RunSummary & { attributes?: Record<string, unknown>; profile?: Record<string, unknown> };
  spans: Span[];
  findings: Report | null;
}

export interface GraphNode { id: string; type: string; label: string; calls: number; errors: number; runs?: number; error_rate?: number }
export interface GraphEdge { source: string; target: string; calls: number; errors: number; steps?: number[]; kind?: string }
export interface AgentGraph { nodes: GraphNode[]; edges: GraphEdge[]; runs?: number }

export interface ClaimEvidence { unit: string; kind: string; source: string; step: number; span_id: string; score: number; matched: string[]; stale: boolean }
export interface Claim { id: string; text: string; start: number; end: number; label: string; score: number; rationale: string; evidence: ClaimEvidence[]; origin: { step: number; span_id: string } | null }
export interface Claims {
  answer: string;
  claims: Claim[];
  segments: { start: number; end: number; claim: string | null; support: number | null }[];
  summary: { claims: number; supported: number; partial: number; stale: number; unsupported: number; contradicted: number; hallucination_score: number; judge: boolean };
}

export interface Incident {
  id: number; project: string; signature: string; agent: string; category: string; target: string | null; title: string;
  root_cause: string; suggested_fix: string; status: string; severity: string; run_count: number;
  first_seen: number; last_seen: number; resolved_at: number | null;
  timeline?: { events: { at: number; kind: string; detail: Record<string, unknown> }[]; runs_per_hour: { hour_unix: number; runs: number }[] };
  affected_runs?: string[];
}

export interface DriftReport {
  id: number; project: string; agent: string; dimension: string; baseline: string; candidate: string;
  verdict: string; severity: string; created_at: number;
  report: {
    headline: string; signals: string[]; runs: { a: number; b: number };
    success_rate: { a: number; b: number; p_value: number | null };
    tool_mix?: { jsd: number; tools: { tool: string; calls_per_run_a: number; calls_per_run_b: number; error_rate_a: number | null; error_rate_b: number | null; status: string }[] };
    steps?: { a: number; b: number; change: number }; tokens?: { a: number; b: number; change: number }; duration_ms?: { a: number; b: number; change: number };
    root_causes?: { a: Record<string, number>; b: Record<string, number> };
    examples?: { a: string[]; b: string[] };
  };
}

export interface AgentStats {
  runs: number; successes: number; failures: number; success_rate: number | null; failure_rate: number | null;
  latency_ms: { p50: number | null; p95: number | null }; steps_per_run: number | null;
  tokens: { total: number; per_run: number | null };
  cost_usd: { total: number; per_task: number | null; per_successful_task: number | null; runs_without_price: number };
  hallucination_rate: number | null; response_quality: number | null; feedback_score: number | null;
  top_root_causes: [string, number][];
}
export interface Analytics {
  window_days: number; project: string | null; agent: string | null; overall: AgentStats; agents: Record<string, AgentStats>;
  tools: { tool: string; calls: number; errors: number; error_rate: number; latency_ms: { p50: number; p95: number } }[];
  retrieval: { source: string; queries: number; empty_rate: number; low_score_rate: number; stale_rate: number; avg_max_score: number | null; reached_answer_rate: number | null }[];
  daily: { day: string; runs: number; failures: number; cost_usd: number; cost_by_agent?: Record<string, number> }[];
  models?: { model: string; calls: number; input_tokens: number; output_tokens: number; cost_usd: number | null; unpriced_calls: number }[];
  prices_configured: string[];
}

export interface DiffRow { step: number; change: string; a: { name: string; kind: string; status: string; output: string | null } | null; b: { name: string; kind: string; status: string; output: string | null } | null }
export interface Diff { a: string; b: string; outcome_a: Outcome; outcome_b: Outcome; first_divergence: number | null; steps: DiffRow[] }
