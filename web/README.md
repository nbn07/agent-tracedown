# Tracedown web app

The Tracedown UI is built with Next.js 16 (App Router), React 19, Tailwind CSS 4, React Flow 12 and recharts 3. The app has no data of its own: every page reads from the Tracedown API server.

## Pages

| Route | What it shows |
|---|---|
| `/runs` | Runs list with filters for project, agent, outcome, fault and search. Each run shows its root cause, fault verdict and hallucination score. |
| `/runs/[id]` | Run detail: an agent stack trace and the root cause with a suggested fix. Tabs: step graph (agent lanes, data flow, cause path highlighted), timeline, findings, claims heatmap and replay. A side panel shows the payloads of the selected step. |
| `/network` | Cross-run agent network: agents, tools, models and handoffs, showing calls and error rates. |
| `/incidents`, `/incidents/[id]` | Incidents grouped by root cause, each with a timeline, runs per hour, affected runs and acknowledge/resolve actions. |
| `/drift` | Drift reports, plus "compare any two versions" (prompt, model or agent version). |
| `/analytics` | Runs, failures and cost per day; tool error rates; tables for agents, tools and retrieval sources. |
| `/live` | Live feed of spans, runs and drift alerts over Server-Sent Events. |
| `/diff?a=&b=` | Step-by-step comparison of two runs, typically a failure and its replay. |

The app has light and dark themes (system, light or dark, set from the sidebar) and works down to phone width.

## Develop

```bash
npm install
TDN_API_URL=http://localhost:7070 npm run dev     # http://localhost:3000
npm run typecheck
```

To get a server with sample data, start the API with the demo option (see the top-level README).

## Build and run

```bash
npm run build                                     # output: 'standalone'
cp -r .next/static .next/standalone/.next/static
cp -r public .next/standalone/public 2>/dev/null || true
cd .next/standalone && PORT=3000 TDN_API_URL=http://tracedown-server:7070 node server.js
```

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `TDN_API_URL` | `http://localhost:7070` | Where `/api`, `/v1`, `/auth` and `/healthz` are forwarded. It is read when a request arrives (in `proxy.ts`), so one built image works in any environment. |
| `PORT` | `3000` | Port for the standalone server. |

## Behind the ingress

In production the ingress sends `/api`, `/v1`, `/auth` and `/healthz` straight to the API server, and everything else to this app. The browser therefore always calls the same origin. The app's own proxy covers local runs and setups without an ingress, and it streams SSE (`/api/live`) unbuffered.

## Auth

- **Open server:** when the server runs without auth, the app needs nothing.
- **API key:** open **Settings** (the key icon in the sidebar) and paste a key. The key is checked against `/api/me`, stored in this browser's localStorage, and sent as `Authorization: Bearer …` on every call, including the live stream.
- **SSO:** when the server has OIDC set up, the Settings dialog links to `/auth/oidc/login`.

A 401 or 403 response shows a message pointing to Settings.

## Screenshots

`docs-screenshots/` holds screenshots taken with Playwright against the demo data, in light mode, some in dark mode, and one at phone width.

## Known gaps in the API

- `/api/runs` does not return each run's fault verdict. For failed runs on screen, the runs page fetches it from `/api/runs/{id}`, four requests at a time.
- There is no endpoint that lists projects or agents. Filter options come from `/api/runs?limit=500`.
- Admin screens for users, API keys, webhooks and PII policies are not built yet. Use the API for those.
