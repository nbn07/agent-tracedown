import type { NextConfig } from "next";

// The browser always calls same-origin /api/v1/... and /v1/...; proxy.ts forwards them to
// TDN_API_URL at request time. In Kubernetes the ingress routes /api and /v1 to the server.
//
// TDN_STATIC_EXPORT=1 builds plain files instead (scripts/build-static.mjs): the Python server
// serves them itself, so `tracedown up` needs no Node at run time.
const exporting = process.env.TDN_STATIC_EXPORT === "1";

const config: NextConfig = exporting
  ? { output: "export", trailingSlash: true, images: { unoptimized: true }, reactStrictMode: true }
  : { output: "standalone", reactStrictMode: true, poweredByHeader: false };

export default config;
