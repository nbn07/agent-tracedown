import { cn } from "@/lib/cn";
import type { ReactNode } from "react";

export type Tone = "neutral" | "ok" | "bad" | "warn" | "cause" | "accent";
const tones: Record<Tone, string> = {
  neutral: "bg-sunken text-muted",
  ok: "bg-ok-soft text-ok",
  bad: "bg-bad-soft text-bad",
  warn: "bg-warn-soft text-warn",
  cause: "bg-cause-soft text-cause",
  accent: "bg-accent-soft text-accent",
};

export function Badge({ tone = "neutral", children, className, title }: { tone?: Tone; children: ReactNode; className?: string; title?: string }) {
  return (
    <span title={title} className={cn("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold leading-4 whitespace-nowrap", tones[tone], className)}>
      {children}
    </span>
  );
}

export const outcomeTone = (o: string): Tone => (o === "success" ? "ok" : o === "failure" ? "bad" : "neutral");
export const severityTone = (s: string): Tone => (s === "critical" || s === "high" ? "bad" : s === "medium" ? "warn" : "neutral");
export const verdictTone = (v: string): Tone => (v === "agent" ? "cause" : v === "user" ? "accent" : v === "unfinished" ? "warn" : "neutral");
