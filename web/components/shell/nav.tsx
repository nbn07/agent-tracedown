"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Activity, AlertTriangle, BarChart3, FileUp, GitCompare, ListTree, Network, Radio, Menu, X } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/cn";
import { useReadOnly } from "@/lib/read-only";
import { ThemeToggle } from "./theme";
import { SettingsButton } from "./settings";

const items = [
  { href: "/runs", label: "Runs", icon: ListTree },
  { href: "/network", label: "Agent network", icon: Network },
  { href: "/incidents", label: "Incidents", icon: AlertTriangle },
  { href: "/drift", label: "Drift", icon: Activity },
  { href: "/analytics", label: "Analytics", icon: BarChart3 },
  { href: "/live", label: "Live", icon: Radio },
  { href: "/diff", label: "Compare runs", icon: GitCompare },
  { href: "/import", label: "Diagnose a trace", icon: FileUp },
];

function Brand() {
  return (
    <Link href="/runs" className="flex items-center gap-2" aria-label="Tracedown home">
      {/* the Tracedown mark (docs/brand): a trace drops down two steps to the cause */}
      <svg width="24" height="24" viewBox="0 0 64 64" aria-hidden>
        <rect x="4" y="4" width="56" height="56" rx="15" fill="#F26419" />
        <line x1="32" y1="15" x2="32" y2="34.5" stroke="#fff" strokeWidth="4.5" strokeLinecap="round" />
        <circle cx="32" cy="15" r="4.2" fill="#fff" />
        <circle cx="32" cy="25.5" r="4.2" fill="#fff" />
        <circle cx="32" cy="44" r="9" fill="none" stroke="#fff" strokeWidth="4.5" />
        <circle cx="32" cy="44" r="3.1" fill="#fff" />
      </svg>
      <span className="font-display text-xl font-semibold tracking-wide">Tracedown</span>
    </Link>
  );
}

function DemoNote() {
  const readOnly = useReadOnly();
  if (!readOnly) return null;
  return (
    <p className="rounded-md border border-line bg-sunken px-2.5 py-2 text-xs leading-5 text-muted">
      <span className="font-medium text-ink">Read-only demo.</span> Look around freely. To diagnose your own runs:{" "}
      <code className="font-mono text-[11px]">pip install agent-tracedown-server</code>
    </p>
  );
}

export function Nav() {
  const path = usePathname();
  const [open, setOpen] = useState(false);
  const links = (
    <nav aria-label="Sections" className="flex flex-col gap-0.5">
      {items.map(({ href, label, icon: Icon }) => {
        const active = path === href || path.startsWith(href + "/");
        return (
          <Link key={href} href={href} onClick={() => setOpen(false)} aria-current={active ? "page" : undefined}
            className={cn("flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm font-medium",
              active ? "bg-sunken text-ink" : "text-muted hover:text-ink hover:bg-sunken")}>
            <Icon size={16} className={active ? "text-cause" : ""} aria-hidden />{label}
          </Link>
        );
      })}
    </nav>
  );
  return (
    <>
      {/* desktop rail */}
      <aside className="hidden md:flex fixed inset-y-0 left-0 w-56 flex-col border-r border-line bg-panel px-3 py-4 gap-6">
        <div className="px-1.5"><Brand /></div>
        {links}
        <DemoNote />
        <div className="mt-auto flex items-center justify-between px-1">
          <span className="text-[11px] text-muted">v0.3</span>
          <div className="flex"><SettingsButton /><ThemeToggle /></div>
        </div>
      </aside>
      {/* mobile bar */}
      <header className="md:hidden sticky top-0 z-30 flex items-center justify-between border-b border-line bg-panel px-4 py-2">
        <Brand />
        <div className="flex items-center">
          <SettingsButton /><ThemeToggle />
          <button className="rounded-md p-2 text-muted cursor-pointer" aria-label={open ? "Close menu" : "Open menu"} aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? <X size={18} /> : <Menu size={18} />}
          </button>
        </div>
      </header>
      {open && <div className="md:hidden border-b border-line bg-panel px-3 py-2">{links}</div>}
    </>
  );
}
