"use client";
import { useEffect, useState } from "react";

// Whether the server refuses writes (`tracedown up --read-only`, the public demo).
// Asked once per page load and shared by every component.
let cached: Promise<boolean> | null = null;

export function useReadOnly(): boolean {
  const [ro, setRo] = useState(false);
  useEffect(() => {
    cached ??= fetch("/healthz", { cache: "no-store" }).then(r => r.json()).then(h => Boolean(h.read_only)).catch(() => false);
    let live = true;
    cached.then(v => { if (live) setRo(v); });
    return () => { live = false; };
  }, []);
  return ro;
}
