"use client";
// Server-Sent Events over fetch, so the Authorization header can be sent
// (EventSource cannot set headers). Reconnects with backoff.
import { authHeaders } from "./api";

export interface SseEvent { type: string; data: unknown }

export function subscribe(path: string, onEvent: (e: SseEvent) => void, onStatus: (s: "connecting" | "open" | "error") => void) {
  let stopped = false;
  let ctrl: AbortController | null = null;
  let retry = 1000;

  async function connect() {
    while (!stopped) {
      onStatus("connecting");
      ctrl = new AbortController();
      try {
        const res = await fetch(`/api/v1${path}`, { headers: { Accept: "text/event-stream", ...authHeaders() }, signal: ctrl.signal, cache: "no-store" });
        if (!res.ok || !res.body) throw new Error(String(res.status));
        onStatus("open");
        retry = 1000;
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let i;
          while ((i = buf.indexOf("\n\n")) >= 0) {
            const block = buf.slice(0, i);
            buf = buf.slice(i + 2);
            let type = "message";
            const data: string[] = [];
            for (const line of block.split("\n")) {
              if (line.startsWith("event:")) type = line.slice(6).trim();
              else if (line.startsWith("data:")) data.push(line.slice(5).trim());
            }
            if (!data.length) continue;
            try { onEvent({ type, data: JSON.parse(data.join("\n")) }); } catch { onEvent({ type, data: data.join("\n") }); }
          }
        }
      } catch {
        if (stopped) return;
        onStatus("error");
      }
      if (stopped) return;
      await new Promise(r => setTimeout(r, retry));
      retry = Math.min(retry * 2, 15000);
    }
  }
  connect();
  return () => { stopped = true; ctrl?.abort(); };
}
