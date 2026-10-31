import { Bot, Brain, Wrench, Search, Database, GitBranch, ArrowRightLeft, Binary, Circle } from "lucide-react";
import type { LucideIcon } from "lucide-react";

export const KIND: Record<string, { color: string; icon: LucideIcon; label: string }> = {
  agent: { color: "var(--k-agent)", icon: Bot, label: "Agent" },
  llm: { color: "var(--k-llm)", icon: Brain, label: "Model call" },
  tool: { color: "var(--k-tool)", icon: Wrench, label: "Tool" },
  retrieval: { color: "var(--k-retrieval)", icon: Search, label: "Retrieval" },
  memory: { color: "var(--k-memory)", icon: Database, label: "Memory" },
  decision: { color: "var(--k-decision)", icon: GitBranch, label: "Decision" },
  handoff: { color: "var(--k-handoff)", icon: ArrowRightLeft, label: "Handoff" },
  embedding: { color: "var(--k-embedding)", icon: Binary, label: "Embedding" },
};
export const kindOf = (k: string) => KIND[k] || { color: "var(--muted)", icon: Circle, label: k };

export const stepOf = (s: { attributes: Record<string, unknown> }) => Number(s.attributes["tdn.step"] ?? 0);
export const shortName = (name: string) => name.replace(/^(invoke_agent|execute_tool|chat)\s+/, "");
