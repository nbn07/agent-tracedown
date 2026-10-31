"use client";
import * as T from "@radix-ui/react-tabs";
import type { ReactNode } from "react";

export function Tabs({ tabs, value, onValueChange }: {
  tabs: { id: string; label: ReactNode; content: ReactNode }[]; value?: string; onValueChange?: (v: string) => void;
}) {
  return (
    <T.Root value={value} onValueChange={onValueChange} defaultValue={tabs[0]?.id}>
      <T.List className="flex gap-1 border-b border-line overflow-x-auto" aria-label="Views">
        {tabs.map(t => (
          <T.Trigger key={t.id} value={t.id}
            className="px-3 py-2 text-sm font-medium text-muted border-b-2 border-transparent -mb-px whitespace-nowrap cursor-pointer hover:text-ink data-[state=active]:text-ink data-[state=active]:border-ink">
            {t.label}
          </T.Trigger>
        ))}
      </T.List>
      {tabs.map(t => <T.Content key={t.id} value={t.id} className="pt-4 focus-visible:outline-none">{t.content}</T.Content>)}
    </T.Root>
  );
}
