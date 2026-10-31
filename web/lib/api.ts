"use client";
// Same-origin API client. The API key (optional; the server can run open in dev) lives in
// localStorage and is sent as a Bearer token. SSO sessions ride on the cookie.
import { useCallback, useEffect, useRef, useState } from "react";

const KEY = "tdn.apiKey";

export function getApiKey(): string {
  try { return localStorage.getItem(KEY) || ""; } catch { return ""; }
}
export function setApiKey(v: string) {
  try { v ? localStorage.setItem(KEY, v) : localStorage.removeItem(KEY); } catch { /* storage unavailable */ }
  window.dispatchEvent(new Event("tdn:auth"));
}

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export function authHeaders(): Record<string, string> {
  const k = getApiKey();
  return k ? { Authorization: `Bearer ${k}` } : {};
}

export async function api<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api/v1${path}`, {
    ...init,
    headers: { ...(init.body ? { "Content-Type": "application/json" } : {}), ...authHeaders(), ...(init.headers || {}) },
    credentials: "same-origin",
    cache: "no-store",
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = j.detail || msg; } catch { /* not JSON */ }
    throw new ApiError(res.status, typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  const ct = res.headers.get("content-type") || "";
  return (ct.includes("json") ? res.json() : res.text()) as Promise<T>;
}

/** Fetch on mount and whenever `path` or the API key changes. `path: null` skips the request. */
export function useApi<T>(path: string | null) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [loading, setLoading] = useState(Boolean(path));
  const [tick, setTick] = useState(0);
  const seq = useRef(0);

  useEffect(() => {
    const onAuth = () => setTick(t => t + 1);
    window.addEventListener("tdn:auth", onAuth);
    return () => window.removeEventListener("tdn:auth", onAuth);
  }, []);

  useEffect(() => {
    if (!path) { setLoading(false); return; }
    const my = ++seq.current;
    setLoading(true);
    setError(null);
    api<T>(path)
      .then(d => { if (my === seq.current) setData(d); })
      .catch(e => { if (my === seq.current) setError(e); })
      .finally(() => { if (my === seq.current) setLoading(false); });
  }, [path, tick]);

  const reload = useCallback(() => setTick(t => t + 1), []);
  return { data, error, loading, reload };
}
