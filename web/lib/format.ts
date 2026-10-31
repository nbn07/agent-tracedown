export const short = (id: string | null | undefined, n = 8) => (id ? id.slice(0, n) : "—");

export function pct(v: number | null | undefined, digits = 0): string {
  return v == null || Number.isNaN(v) ? "—" : `${(v * 100).toFixed(digits)}%`;
}

export function ms(v: number | null | undefined): string {
  if (v == null) return "—";
  if (v < 1) return `${v.toFixed(2)} ms`;
  if (v < 1000) return `${v.toFixed(0)} ms`;
  return `${(v / 1000).toFixed(v < 10000 ? 1 : 0)} s`;
}

export function usd(v: number | null | undefined): string {
  if (v == null) return "—";
  if (v === 0) return "$0";
  if (v < 0.01) return `$${v.toFixed(5)}`;
  return `$${v.toFixed(2)}`;
}

/** A run's cost: "$0.012", "$0.012+" when some model calls had no price, "—" when none did. */
export function runCost(r: { cost_usd?: number | null; cost_complete?: boolean }): string {
  if (r.cost_usd == null) return "—";
  return usd(r.cost_usd) + (r.cost_complete === false ? "+" : "");
}

export function num(v: number | null | undefined): string {
  return v == null ? "—" : v.toLocaleString("en-US", { maximumFractionDigits: 1 });
}

/** unix nanoseconds -> Date */
export const fromNs = (ns: number) => new Date(ns / 1e6);

export function ago(ns: number | null | undefined): string {
  if (!ns) return "—";
  const s = (Date.now() - ns / 1e6) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return fromNs(ns).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function when(ns: number | null | undefined): string {
  if (!ns) return "—";
  return fromNs(ns).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export const humanize = (s: string) => s.replace(/_/g, " ");
