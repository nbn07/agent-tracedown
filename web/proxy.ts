import { NextResponse, type NextRequest } from "next/server";

// Forward API calls to the Tracedown server. Read at request time, so one image works with
// any TDN_API_URL (next.config rewrites are fixed at build time in a standalone build).
// Behind the Kubernetes ingress /api and /v1 never reach this app; this covers local
// development, docker compose and running the web container on its own.
export function proxy(req: NextRequest) {
  const api = (process.env.TDN_API_URL || "http://localhost:7070").replace(/\/$/, "");
  const url = new URL(req.nextUrl.pathname + req.nextUrl.search, api);
  return NextResponse.rewrite(url);
}

export const config = { matcher: ["/api/:path*", "/v1/:path*", "/auth/:path*", "/healthz"] };
