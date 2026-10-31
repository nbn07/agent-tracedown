import type { Span } from "@/lib/types";
import { kindOf, shortName, stepOf } from "@/lib/kinds";
import { ms } from "@/lib/format";

export function Waterfall({ spans, selected, onSelect, cause }: { spans: Span[]; selected: string | null; onSelect: (id: string) => void; cause: string | null }) {
  const sorted = [...spans].sort((a, b) => stepOf(a) - stepOf(b));
  const t0 = Math.min(...spans.map(s => s.start_time));
  const t1 = Math.max(...spans.map(s => s.end_time || s.start_time));
  const span = Math.max(1, t1 - t0);
  const depth: Record<string, number> = {};
  const byId = Object.fromEntries(spans.map(s => [s.span_id, s]));
  const d = (s: Span): number => (depth[s.span_id] ??= s.parent_span_id && byId[s.parent_span_id] ? d(byId[s.parent_span_id]) + 1 : 0);
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] text-sm">
        <tbody>
          {sorted.map(s => {
            const k = kindOf(s.kind);
            const left = ((s.start_time - t0) / span) * 100;
            const width = Math.max(0.6, (((s.end_time || s.start_time) - s.start_time) / span) * 100);
            const err = s.status.code === "ERROR";
            return (
              <tr key={s.span_id} onClick={() => onSelect(s.span_id)}
                className={`cursor-pointer border-b border-line last:border-0 ${selected === s.span_id ? "bg-accent-soft" : "hover:bg-sunken/60"}`}>
                <td className="w-10 py-1.5 pl-2 font-mono text-xs text-muted tabular">{stepOf(s)}</td>
                <td className="w-[38%] py-1.5">
                  <div className="flex items-center gap-1.5 truncate" style={{ paddingLeft: d(s) * 14 }}>
                    <k.icon size={13} style={{ color: k.color }} aria-hidden />
                    <span className={`truncate ${cause === s.span_id ? "font-semibold text-cause" : ""}`}>{shortName(s.name)}</span>
                    {err && <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-bad" aria-label="error" />}
                  </div>
                </td>
                <td className="py-1.5 pr-2">
                  <div className="relative h-3.5 rounded bg-sunken">
                    <div className="absolute top-0 h-full rounded" title={ms(((s.end_time || s.start_time) - s.start_time) / 1e6)}
                      style={{ left: `${left}%`, width: `${width}%`, background: cause === s.span_id ? "var(--cause)" : err ? "var(--bad)" : k.color, opacity: 0.85 }} />
                  </div>
                </td>
                <td className="w-20 py-1.5 pr-2 text-right font-mono text-xs text-muted tabular">{ms(((s.end_time || s.start_time) - s.start_time) / 1e6)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
