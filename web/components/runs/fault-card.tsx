import type { Fault } from "@/lib/types";
import { VerdictBadge, verdictHelp } from "@/components/verdict";

export function FaultCard({ fault }: { fault: Fault }) {
  const border = fault.verdict === "agent" ? "border-cause/40" : fault.verdict === "user" ? "border-accent/40" : "border-warn/40";
  return (
    <div className={`rounded-lg border ${border} bg-panel p-4`}>
      <div className="eyebrow">Who is at fault?</div>
      <div className="mt-1.5 flex flex-wrap items-center gap-2">
        <VerdictBadge fault={fault} />
        <span className="font-medium">{verdictHelp[fault.verdict]}</span>
        <span className="text-xs text-muted">{fault.source === "llm_judge" ? "from the LLM judge" : "from the rules"}</span>
      </div>
      <p className="mt-2 text-sm text-ink/85">{fault.reason}</p>
      {fault.hints && fault.hints.length > 0 && (
        <ul className="mt-2 list-disc pl-5 text-xs text-muted">{fault.hints.map((h, i) => <li key={i}>{h}</li>)}</ul>
      )}
      {fault.source === "llm_judge" && <p className="mt-2 text-xs text-muted">Decided by the LLM judge: treat it as a lead to check, not a ruling.</p>}
    </div>
  );
}
