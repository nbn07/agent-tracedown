"use client";
import { useState } from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";

interface Plan { mode: string; from_step: number | null; steps: { step: number; kind: string; name: string; source: string }[]; recorded: number; live: number; replayable: boolean; missing_payload_steps: number[]; sdk_snippet: string }

export function ReplayPlanner({ traceId, defaultStep, steps }: { traceId: string; defaultStep: number | null; steps: number[] }) {
  const [mode, setMode] = useState(defaultStep != null ? "from-step" : "exact");
  const [step, setStep] = useState<number | null>(defaultStep ?? steps[0] ?? null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const run = () => api<Plan>(`/runs/${traceId}/replay`, { method: "POST", body: JSON.stringify({ mode, from_step: mode === "from-step" ? step : null }) })
    .then(p => { setPlan(p); setErr(null); }).catch(e => setErr(e.message));
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted">Plan a replay: earlier steps come from the recording, and from the chosen step on the agent runs live with your fix.</p>
      <div className="flex flex-wrap items-end gap-3">
        <Select label="Mode" value={mode} onChange={e => setMode(e.target.value)}>
          <option value="exact">Exact (all recorded)</option><option value="from-step">From a step</option>
          <option value="swap">Swap model or prompt</option><option value="full">Fully live</option>
        </Select>
        {mode === "from-step" && (
          <Select label="From step" value={step ?? ""} onChange={e => setStep(Number(e.target.value))}>
            {steps.map(s => <option key={s} value={s}>{s}</option>)}
          </Select>
        )}
        <Button onClick={run}>Plan replay</Button>
      </div>
      {err && <p className="text-sm text-bad">{err}</p>}
      {plan && (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-2 text-xs">
            <Badge tone={plan.replayable ? "ok" : "bad"}>{plan.replayable ? "replayable" : "not replayable"}</Badge>
            <Badge>{plan.recorded} recorded</Badge><Badge tone="accent">{plan.live} live</Badge>
            {plan.missing_payload_steps.length > 0 && <Badge tone="warn">payloads missing at steps {plan.missing_payload_steps.join(", ")}</Badge>}
          </div>
          <ol className="flex flex-wrap gap-1.5 text-xs">
            {plan.steps.map(s => <li key={s.step} className={`rounded border px-2 py-1 ${s.source === "live" ? "border-accent text-accent" : "border-line text-muted"}`}>#{s.step} {s.name} · {s.source}</li>)}
          </ol>
          <pre className="overflow-x-auto rounded-md bg-sunken p-3 font-mono text-xs">{plan.sdk_snippet}</pre>
        </div>
      )}
    </div>
  );
}
