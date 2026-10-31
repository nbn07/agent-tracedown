"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import { useReadOnly } from "@/lib/read-only";
import { FileUp } from "lucide-react";
import { ApiError, authHeaders } from "@/lib/api";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Badge, outcomeTone } from "@/components/ui/badge";

interface Imported { trace_id: string; agent: string | null; outcome: string | null; root_cause: string | null; step: number | null }
interface Result { format: string; project: string; runs: Imported[]; skipped: number }

const FORMATS: Record<string, string> = { langfuse: "Langfuse export", otlp: "OpenTelemetry (OTLP/JSON) export", tracedown: "Tracedown bundle" };
const SAMPLES = [
  { label: "Langfuse sample", path: "/samples/langfuse-trace.json" },
  { label: "OpenTelemetry sample", path: "/samples/otel-trace.json" },
];

async function post(text: string): Promise<Result> {
  const res = await fetch("/api/v1/import?project=pasted", {
    method: "POST", body: text, credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...authHeaders() },
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch { /* not JSON */ }
    throw new ApiError(res.status, String(msg));
  }
  return res.json();
}

export function ImportPage() {
  const router = useRouter();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [dragging, setDragging] = useState(false);
  const file = useRef<HTMLInputElement>(null);
  const readOnly = useReadOnly();

  const run = async (body: string) => {
    setBusy(true); setError(null); setResult(null);
    try {
      const r = await post(body);
      if (r.runs.length === 1) router.push(`/runs/${encodeURIComponent(r.runs[0].trace_id)}`);
      else setResult(r);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  const readFile = async (f: File | undefined) => {
    if (!f) return;
    const body = await f.text();
    setText(body.length > 200_000 ? body.slice(0, 200_000) + "\n…" : body);
    run(body);
  };
  const sample = async (path: string) => {
    const body = await (await fetch(path)).text();
    setText(body);
    run(body);
  };

  return (
    <div className="max-w-4xl">
      <PageHeader title="Diagnose a trace"
        sub="Paste or drop a run you already recorded. Tracedown finds the step that caused the failure. Nothing to install in your agent." />

      {readOnly && (
        <div className="mb-4 rounded-lg border border-line bg-sunken p-4 text-sm">
          <p className="font-medium">This public demo is read-only, so it cannot take your traces.</p>
          <p className="mt-1 text-muted">Run Tracedown on your own machine instead. Your traces stay there:</p>
          <pre className="mt-2 overflow-x-auto rounded-md bg-bg p-2 font-mono text-xs">pip install agent-tracedown-server && tracedown up</pre>
          <p className="mt-2 text-muted">Then open &ldquo;Diagnose a trace&rdquo; there. The runs already in this demo show what you get.</p>
        </div>
      )}

      {!readOnly && <div
        onDragOver={e => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={e => { e.preventDefault(); setDragging(false); readFile(e.dataTransfer.files[0]); }}
        className={`rounded-lg border bg-panel p-3 ${dragging ? "border-accent" : "border-line"}`}>
        <label htmlFor="trace" className="eyebrow mb-2 block">Trace JSON</label>
        <textarea id="trace" value={text} onChange={e => setText(e.target.value)} spellCheck={false}
          placeholder="Paste a Langfuse trace export, an OpenTelemetry OTLP/JSON export, or a Tracedown bundle. Or drop the file here."
          className="h-64 w-full resize-y rounded-md border border-line bg-bg p-3 font-mono text-xs leading-5 outline-none focus:border-ink/40" />
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Button variant="primary" disabled={busy || !text.trim()} onClick={() => run(text)}>{busy ? "Diagnosing…" : "Diagnose"}</Button>
          <Button onClick={() => file.current?.click()} disabled={busy}><FileUp size={14} aria-hidden />Choose a file</Button>
          <input ref={file} type="file" accept=".json,.jsonl,application/json" className="hidden"
            onChange={e => readFile(e.target.files?.[0])} />
          <span className="ml-auto flex flex-wrap items-center gap-2 text-xs text-muted">
            No file at hand? Try
            {SAMPLES.map(s => (
              <button key={s.path} onClick={() => sample(s.path)} disabled={busy}
                className="cursor-pointer text-accent underline underline-offset-2">{s.label}</button>
            ))}
          </span>
        </div>
      </div>}

      {error && <p role="alert" className="mt-4 rounded-md border border-bad/40 bg-bad-soft p-3 text-sm text-bad">{error}</p>}

      {result && (
        <div className="mt-5">
          <p className="mb-2 text-sm text-muted">
            Read a {FORMATS[result.format] || result.format}: {result.runs.length} run{result.runs.length === 1 ? "" : "s"}
            {result.skipped > 0 && <>, {result.skipped} more not imported (50 at a time)</>}.
          </p>
          <ul className="divide-y divide-line rounded-lg border border-line bg-panel">
            {result.runs.map(r => (
              <li key={r.trace_id} className="flex flex-wrap items-center gap-3 px-3 py-2 text-sm">
                {r.outcome && <Badge tone={outcomeTone(r.outcome)}>{r.outcome}</Badge>}
                <Link href={`/runs/${encodeURIComponent(r.trace_id)}`} className="font-medium hover:underline">{r.agent || r.trace_id}</Link>
                <span className="text-muted">{r.root_cause ? `${r.root_cause} · step ${r.step}` : "nothing found"}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="mt-8 space-y-2 text-sm text-muted">
        <p><span className="font-medium text-ink">Where to get a trace.</span> Langfuse: open a trace and use the download button, or <code className="font-mono text-xs">GET /api/public/traces/&lt;id&gt;</code>. OpenTelemetry: any OTLP/JSON file, for example from the collector&apos;s file exporter or Arize Phoenix.</p>
        <p><span className="font-medium text-ink">What happens to it.</span> It is stored on this Tracedown server, in the project &ldquo;pasted&rdquo;, like any other run. For many runs at once, use <code className="font-mono text-xs">tracedown-import</code>.</p>
      </div>
    </div>
  );
}
