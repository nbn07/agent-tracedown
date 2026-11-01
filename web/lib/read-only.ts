"use client";
import { useEffect, useState } from "react";

// What /healthz says about the server: its version, and whether it refuses writes
// (`tracedown up --read-only`, the public demo). Asked once per page load.
export interface ServerInfo { version: string | null; readOnly: boolean }
let cached: Promise<ServerInfo> | null = null;

export function useServerInfo(): ServerInfo {
  const [info, setInfo] = useState<ServerInfo>({ version: null, readOnly: false });
  useEffect(() => {
    cached ??= fetch("/healthz", { cache: "no-store" }).then(r => r.json())
      .then(h => ({ version: typeof h.version === "string" ? h.version : null, readOnly: Boolean(h.read_only) }))
      .catch(() => ({ version: null, readOnly: false }));
    let live = true;
    cached.then(v => { if (live) setInfo(v); });
    return () => { live = false; };
  }, []);
  return info;
}

export function useReadOnly(): boolean {
  return useServerInfo().readOnly;
}
