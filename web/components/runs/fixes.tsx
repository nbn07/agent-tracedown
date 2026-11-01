"use client";
import Link from "next/link";
import { useState } from "react";
import { Check, CircleDashed, Copy, FlaskConical, Wrench, X } from "lucide-react";
import { useApi } from "@/lib/api";
import { short } from "@/lib/format";
import { Badge, type Tone } from "@/components/ui/badge";
import { ErrorState, Loading } from "@/components/ui/states";

interface Check { passed: boolean; verdict: string; reasons: string[] }
interface Replay { trace_id: string; suggestion: string | null; check: Check }
export interface Suggestion {
  id: string; key: string; category: string; label: string; step: number; title: string; change: string;
  try: { from_step: number } | null; tries_as: string | null; why_not_tried: string | null;
  status: "works" | "does_not_work" | "untested" | "not_tried"; result?: string; replays: Replay[];
}
interface Suggestions { suggestions: Suggestion[]; other_replays: Replay[] }

const STATUS: Record<Suggestion["status"], { label: string; tone: Tone; icon: typeof Check }> = {
  works: { label: "Works", tone: "ok", icon: Check },
  does_not_work: { label: "Did not work", tone: "bad", icon: X },
  untested: { label: "Not tested yet", tone: "neutral", icon: CircleDashed },
  not_tried: { label: "Needs a code change", tone: "warn", icon: Wrench },
};

function snippet(traceId: string, origin: string, tools: string[]) {
  const stubs = tools.length
    ? `{${tools.slice(0, 3).map(t => `"${t}": fake_${t.replace(/\W/g, "_")}`).join(", ")}}`
    : `{}`;
  return `import agent_tracedown as tdn

tdn.init(endpoint="${origin}")
results = tdn.try_fixes(
    "${traceId}",
    agent=lambda: my_agent("the same input as the failed run"),  # run your agent once
    tools=${stubs},  # stand-ins, so nothing runs for real
)
for r in results:
    print(r["verdict"], "-", r["change"])`;
}

function CopyBlock({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => navigator.clipboard?.writeText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); });
  return (
    <div className="relative">
      <pre className="overflow-x-auto rounded-md bg-sunken p-3 pr-10 font-mono text-xs">{text}</pre>
      <button onClick={copy} title="Copy" className="absolute right-2 top-2 cursor-pointer rounded p-1 text-muted hover:text-ink">
        {copied ? <Check size={14} /> : <Copy size={14} />}
      </button>
    </div>
  );
}

function ReplayLine({ original, r }: { original: string; r: Replay }) {
  return (
    <li className="flex flex-wrap items-baseline gap-2">
      <Badge tone={r.check.passed ? "ok" : "bad"}>{r.check.passed ? "passed" : "failed"}</Badge>
      <Link className="font-mono text-accent underline underline-offset-2" href={`/diff?a=${original}&b=${r.trace_id}`}>{short(r.trace_id)}</Link>
      <span className="text-muted">{r.check.reasons.join("; ")}</span>
    </li>
  );
}

export function SuggestedFixes({ traceId, onStep, tools = [] }: { traceId: string; onStep: (step: number) => void; tools?: string[] }) {
  const { data, error } = useApi<Suggestions>(`/runs/${traceId}/suggestions`);
  if (error) return <ErrorState error={error} />;
  if (!data) return <Loading rows={3} />;
  const { suggestions, other_replays } = data;
  if (!suggestions.length) return <p className="text-sm text-muted">Nothing to fix: Tracedown found no root cause in this run.</p>;
  const tryable = suggestions.some(s => s.try);
  const origin = typeof window !== "undefined" ? window.location.origin : "http://127.0.0.1:7070";
  return (
    <div className="space-y-4">
      <p className="text-sm text-muted">
        What to change, most likely first. Tracedown tries a fix by replaying the run with it, three times, and
        checks that the problem is gone, nothing new broke and the run finished.
      </p>
      <ol className="space-y-3">
        {suggestions.map(s => {
          const st = STATUS[s.status];
          const Icon = st.icon;
          return (
            <li key={s.id} className="rounded-lg border border-line p-4">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={st.tone}><Icon size={12} className="mr-1 inline" />{st.label}</Badge>
                {s.result && <span className="text-xs text-muted">{s.result}</span>}
                <span className="font-semibold">{s.label}</span>
                <button className="cursor-pointer text-sm text-accent underline underline-offset-2" onClick={() => onStep(s.step)}>step {s.step}</button>
              </div>
              <p className="mt-2 text-sm">{s.change}</p>
              <p className="mt-2 flex items-start gap-1.5 text-xs text-muted">
                <FlaskConical size={13} className="mt-0.5 shrink-0" />
                {s.tries_as ? <>Tracedown can try this for you: {s.tries_as}.</> : s.why_not_tried}
              </p>
              {s.replays.length > 0 && <ul className="mt-2 space-y-1 text-xs">{s.replays.map(r => <ReplayLine key={r.trace_id} original={traceId} r={r} />)}</ul>}
            </li>
          );
        })}
      </ol>
      {tryable && (
        <div className="space-y-2">
          <div className="eyebrow">Try them on your agent</div>
          <p className="text-sm text-muted">
            Fixes are tried inside your agent&apos;s code, so run this where your agent runs. Earlier steps come from the
            recording; no tool runs for real unless you give it a stand-in or list it in <code className="font-mono">live_tools</code>.
            Results appear here.
          </p>
          <CopyBlock text={snippet(traceId, origin, tools)} />
        </div>
      )}
      {other_replays.length > 0 && (
        <div className="space-y-2">
          <div className="eyebrow">Your own replays of this run</div>
          <ul className="space-y-1 text-xs">{other_replays.map(r => <ReplayLine key={r.trace_id} original={traceId} r={r} />)}</ul>
        </div>
      )}
    </div>
  );
}

export function FixCheckBanner({ original, replay }: { original: string; replay: string }) {
  const { data } = useApi<Check>(`/runs/${original}/fix-check?replay=${replay}`);
  if (!data || data.verdict === "nothing_to_fix") return null;
  return (
    <div className={`rounded-lg border p-4 ${data.passed ? "border-ok/40 bg-ok-soft/40" : "border-bad/40 bg-bad-soft/40"}`}>
      <div className="eyebrow">Did this replay fix the run?</div>
      <p className={`mt-1 font-semibold ${data.passed ? "text-ok" : "text-bad"}`}>{data.passed ? "Yes: it passes Tracedown's checks" : "No: the fix check failed"}</p>
      <ul className="mt-1 list-disc pl-5 text-sm">{data.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
    </div>
  );
}
