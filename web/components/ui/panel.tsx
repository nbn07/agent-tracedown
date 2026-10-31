import { cn } from "@/lib/cn";
import type { ReactNode } from "react";

export function Panel({ title, action, children, className, bodyClass, eyebrow }: {
  title?: ReactNode; action?: ReactNode; children: ReactNode; className?: string; bodyClass?: string; eyebrow?: string;
}) {
  return (
    <section className={cn("rounded-lg border border-line bg-panel min-w-0", className)}>
      {(title || action) && (
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5">
          <div className="min-w-0">
            {eyebrow && <div className="eyebrow">{eyebrow}</div>}
            {title && <h2 className="text-lg font-semibold leading-tight truncate">{title}</h2>}
          </div>
          {action}
        </header>
      )}
      <div className={cn("p-4", bodyClass)}>{children}</div>
    </section>
  );
}

export function Stat({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: "ok" | "bad" | "warn" }) {
  const color = tone === "ok" ? "text-ok" : tone === "bad" ? "text-bad" : tone === "warn" ? "text-warn" : "text-ink";
  return (
    <div className="rounded-lg border border-line bg-panel px-4 py-3 min-w-0">
      <div className="eyebrow">{label}</div>
      <div className={cn("font-display text-3xl font-semibold tabular leading-tight mt-1", color)}>{value}</div>
      {sub && <div className="text-xs text-muted mt-0.5 truncate">{sub}</div>}
    </div>
  );
}
