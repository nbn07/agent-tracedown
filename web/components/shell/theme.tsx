"use client";
import { Moon, Sun, Monitor } from "lucide-react";
import { useEffect, useState } from "react";

type Mode = "light" | "dark" | "system";
const KEY = "tdn.theme";

// Runs before paint (inlined in <head>) so the page never flashes the wrong theme.
export const themeScript = `(function(){try{var m=localStorage.getItem('${KEY}')||'system';var d=m==='dark'||(m==='system'&&matchMedia('(prefers-color-scheme: dark)').matches);document.documentElement.classList.toggle('dark',d);}catch(e){}})();`;

function apply(mode: Mode) {
  const dark = mode === "dark" || (mode === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
}

export function ThemeToggle() {
  const [mode, setMode] = useState<Mode>("system");
  useEffect(() => {
    let m: Mode = "system";
    try { m = (localStorage.getItem(KEY) as Mode) || "system"; } catch { /* no storage */ }
    setMode(m);
    const mq = matchMedia("(prefers-color-scheme: dark)");
    const on = () => { let cur: Mode = "system"; try { cur = (localStorage.getItem(KEY) as Mode) || "system"; } catch {} if (cur === "system") apply("system"); };
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  const next: Record<Mode, Mode> = { system: "light", light: "dark", dark: "system" };
  const Icon = mode === "light" ? Sun : mode === "dark" ? Moon : Monitor;
  return (
    <button
      onClick={() => { const m = next[mode]; setMode(m); try { localStorage.setItem(KEY, m); } catch {} apply(m); }}
      className="rounded-md p-2 text-muted hover:text-ink hover:bg-sunken cursor-pointer"
      aria-label={`Theme: ${mode}. Switch to ${next[mode]}`} title={`Theme: ${mode}`}>
      <Icon size={16} />
    </button>
  );
}
