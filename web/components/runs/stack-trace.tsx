"use client";
import { Check, Copy } from "lucide-react";
import { useState } from "react";

function lineClass(l: string) {
  if (/^AgentFailureException/.test(l)) return "font-semibold text-ink";
  if (/^\s+caused by/.test(l)) return "text-cause font-semibold";
  if (/^\s+root cause:/.test(l)) return "text-cause";
  if (/^\s+at\s/.test(l)) return "text-bad";
  if (/^\s+fault:/.test(l)) return "text-accent font-medium";
  if (/^\s+(evidence|also|next):/.test(l)) return "text-ink/80";
  if (/^\s+run\s/.test(l)) return "text-muted";
  return "text-ink/80";
}

export function StackTrace({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="relative">
      <button
        onClick={() => { navigator.clipboard?.writeText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); }).catch(() => {}); }}
        className="absolute right-2 top-2 inline-flex items-center gap-1 rounded border border-line bg-panel px-2 py-1 text-xs text-muted hover:text-ink cursor-pointer"
        aria-label="Copy stack trace">
        {copied ? <Check size={12} /> : <Copy size={12} />}{copied ? "Copied" : "Copy"}
      </button>
      <pre className="overflow-x-auto whitespace-pre-wrap break-words rounded-md bg-sunken p-4 pr-20 font-mono text-[12.5px] leading-relaxed">
        {text.split("\n").map((l, i) => <div key={i} className={lineClass(l)}>{l || " "}</div>)}
      </pre>
    </div>
  );
}
