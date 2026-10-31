"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Activity, AlertTriangle, BarChart3, GitCompare, ListTree, Network, Radio, Menu, X } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/cn";
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
];

function Brand() {
  return (
    <Link href="/runs" className="flex items-center gap-2" aria-label="Tracedown home">
      <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden>
        <rect x="2" y="4" width="20" height="16" rx="3" fill="var(--cause)" />
        <path d="M5 13h3l2-4 3 7 2-3h4" stroke="#fff" strokeWidth="1.8" fill="none" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <span className="font-display text-xl font-semibold tracking-wide">Tracedown</span>
    </Link>
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
