import type { SelectHTMLAttributes } from "react";
import { cn } from "@/lib/cn";

export function Select({ className, label, ...props }: SelectHTMLAttributes<HTMLSelectElement> & { label: string }) {
  return (
    <label className="flex flex-col gap-1 text-xs font-medium text-muted min-w-0">
      {label}
      <select className={cn("h-9 rounded-md border border-line bg-panel px-2.5 text-sm text-ink cursor-pointer", className)} {...props} />
    </label>
  );
}
