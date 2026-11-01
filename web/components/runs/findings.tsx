import type { RootCause } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { humanize } from "@/lib/format";

export function Findings({ findings, onStep }: { findings: RootCause[]; onStep: (spanId: string) => void }) {
  if (!findings.length) return <p className="text-sm text-muted">No findings. The run looks healthy to every detector.</p>;
  return (
    <ol className="space-y-3">
      {findings.map((f, i) => (
        <li key={f.id} className={`rounded-lg border p-3 ${f.root_cause ? "border-cause/50 bg-cause-soft/40" : "border-line bg-panel"}`}>
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-xs text-muted" title="Suspects in ranked order. The order is a best guess, not a probability.">#{i + 1}</span>
            <span className="font-semibold">{f.label}</span>
            {f.root_cause && <Badge tone="cause">root cause</Badge>}
            {f.symptom && <Badge tone="bad">symptom</Badge>}
            {f.step != null && (
              <button onClick={() => onStep(f.span_id)} className="text-xs text-accent underline underline-offset-2 cursor-pointer">step {f.step}</button>
            )}
            <span className="ml-auto flex items-center gap-2 text-xs text-muted">
              <span className="hidden sm:inline">found by {humanize(f.detector)}</span>
            </span>
          </div>
          <p className="mt-1 text-sm">{f.title}</p>
          {f.evidence.length > 0 && (
            <ul className="mt-2 space-y-0.5 border-l-2 border-line pl-3 font-mono text-xs text-ink/80">
              {f.evidence.map((e, j) => <li key={j} className="break-words">{e}</li>)}
            </ul>
          )}
        </li>
      ))}
    </ol>
  );
}
