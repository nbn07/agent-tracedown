import type { NextConfig } from "next";

// The browser always calls same-origin /api/v1/... and /v1/...; proxy.ts forwards them to
// TDN_API_URL at request time. In Kubernetes the ingress routes /api and /v1 to the server.
const config: NextConfig = {
  output: "standalone",
  reactStrictMode: true,
  poweredByHeader: false,
};

export default config;
