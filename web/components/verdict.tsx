import { Badge, verdictTone } from "@/components/ui/badge";
import type { Fault } from "@/lib/types";

const LABEL: Record<string, string> = { agent: "Agent", user: "User", unfinished: "Unfinished", unclear: "Unclear" };
const HELP: Record<string, string> = {
  agent: "The agent made a mistake",
  user: "The user caused the failure, not the agent",
  unfinished: "Nothing went wrong yet: the run stopped while the agent was waiting",
  unclear: "No evidence for either side",
};

export function VerdictBadge({ fault }: { fault: Fault | null | undefined }) {
  if (!fault) return <span className="text-muted">—</span>;
  return <Badge tone={verdictTone(fault.verdict)} title={HELP[fault.verdict] || ""}>{LABEL[fault.verdict] || fault.verdict}</Badge>;
}
export const verdictHelp = HELP;
