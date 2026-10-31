import { cn } from "@/lib/cn";
import type { ReactNode } from "react";
import { ApiError } from "@/lib/api";

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("animate-pulse rounded-md bg-sunken", className)} />;
}

export function Loading({ rows = 4 }: { rows?: number }) {
  return (
    <div className="space-y-2" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }).map((_, i) => <Skeleton key={i} className="h-9 w-full" />)}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-line px-6 py-10 text-center">
      <p className="font-medium">{title}</p>
      {children && <div className="mt-1 text-sm text-muted">{children}</div>}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  const status = error instanceof ApiError ? error.status : 0;
  const msg =
    status === 401 ? "This server needs an API key. Add one in Settings (the key icon at the top)." :
    status === 403 ? "Your key doesn't have access to this. Ask an admin for a role on this project." :
    status === 404 ? "Not found. It may have been deleted, or belong to a project you can't see." :
    `The Tracedown API didn't respond as expected (${error.message}). Check that the server is running.`;
  return (
    <div role="alert" className="rounded-lg border border-bad/30 bg-bad-soft px-4 py-3 text-sm text-bad flex items-center justify-between gap-4">
      <span>{msg}</span>
      {onRetry && <button onClick={onRetry} className="underline underline-offset-2 cursor-pointer">Try again</button>}
    </div>
  );
}
