# Deploying Tracedown

Tracedown ships as three processes: **server** (API + OTLP ingest, `tracedown-server`, port 7070), **worker** (Kafka consumer running the analysis pipeline, `tracedown-worker`), and **web** (Next.js standalone, port 3000). All configuration is environment variables prefixed `TDN_`.

| Layout | Metadata | Spans | Ingest | Use for |
|---|---|---|---|---|
| Single container | SQLite (`TDN_DB`) | SQLite | in-process | a laptop, a demo, one team |
| Compose / Helm, standard | Postgres | Postgres | in-process (Redis cache) | most teams, HA API |
| Compose / Helm, scale | Postgres | ClickHouse (1..n clusters) | Kafka + workers | high span volume, data residency |

## 1. Single container

```bash
docker build -f deploy/Dockerfile -t tracedown .
docker run -p 7070:7070 -v tdn-data:/data tracedown
```

The image runs as uid 10001 with `/data` as its working directory; SQLite lives there. One replica only, because SQLite cannot be shared.

## 2. Docker Compose

```bash
cd deploy
docker compose up -d                     # server, web, postgres, redis
docker compose --profile scale up -d     # + worker, redpanda, clickhouse
```

Web is at :3000 and the API at :7070. Change the Postgres password in `docker-compose.yml` (or an `.env` file) before exposing it.

## 3. Kubernetes (Helm)

```bash
helm install tdn deploy/helm/tracedown \
  --set ingress.enabled=true --set ingress.host=tracedown.example.com \
  --set config.publicUrl=https://tracedown.example.com \
  --set secrets.apiKeys='ingest-key-1'
```

What the chart renders:

- **server**: Deployment, Service, HPA, PDB (only when the minimum replicas is more than 1), and startup/readiness/liveness probes on `/healthz`.
- **worker**: Deployment and HPA. Rendered only when Kafka is configured. Probes run on `:8081/healthz`; it drains on SIGTERM within 60 s.
- **web**: Deployment and Service.
- **Ingress**: `/api`, `/v1`, `/auth`, `/healthz`, `/metrics`, `/docs` and `/openapi.json` route to the server; `/` routes to web.
- **Configuration**: a ConfigMap and a Secret. Generated secrets stay stable across upgrades via `lookup`.
- **Migrations**: a hook Job that runs post-install and pre-upgrade.
- **Security**:
  - NetworkPolicy; data services accept traffic only from release pods;
  - a ServiceAccount;
  - non-root containers with a read-only root filesystem, all capabilities dropped, and the RuntimeDefault seccomp profile.

**Data services.** By default the chart bundles single-replica StatefulSets for Postgres and Redis. Redpanda and ClickHouse are bundled too, but disabled by default; turn them on with `redpanda.enabled` and `clickhouse.enabled`. The bundled services are fine for evaluation. In production, bring your own managed services:

```yaml
postgresql: {enabled: false}
redis: {enabled: false}
external:
  databaseUrl: postgresql://tdn:...@pg.internal:5432/tdn
  redisUrl: rediss://cache.internal:6379/0
  kafkaBootstrap: b-1.msk.internal:9092,b-2.msk.internal:9092
  clickhouse: '{"clusters":{"us":{"url":"https://ch-us:8443"},"eu":{"url":"https://ch-eu:8443"}},"routes":{"acme-eu":"eu"},"default":"us"}'
secrets:
  existingSecret: tracedown-secrets   # keys named like the TDN_* variables below
```

If you set `secrets.existingSecret`, that Secret must contain the TDN_* keys the chart would otherwise generate: `TDN_DATABASE_URL`, `TDN_REDIS_URL`, `TDN_KAFKA_BOOTSTRAP`, `TDN_CLICKHOUSE`, `TDN_API_KEYS`, `TDN_SESSION_SECRET` and the OIDC values.

## Environment reference

| Variable | Meaning |
|---|---|
| `TDN_DATABASE_URL` | Postgres URL for metadata (and for spans when ClickHouse is not configured). If unset, SQLite at `TDN_DB` is used. |
| `TDN_REDIS_URL` | Shared cache and rate limits across API replicas. |
| `TDN_CLICKHOUSE` | Span store. Either a single URL, or JSON `{"clusters":{name:{"url","database"?}},"routes":{project:cluster},"default":name}`. `TDN_CLICKHOUSE_CLUSTER` picks the cluster for a single URL. |
| `TDN_KAFKA_BOOTSTRAP` | Send ingest through Kafka; `tracedown-worker` consumes it (`TDN_QUEUE=inprocess` forces a local thread instead). |
| `TDN_KAFKA_TOPIC`, `TDN_KAFKA_GROUP` | Defaults `tdn.spans` / `tracedown-worker`. |
| `TDN_WORKER_HEALTH_PORT` | The worker's `/healthz` port. It returns 503 when the worker has not polled recently. |
| `TDN_API_KEYS`, `TDN_AUTH=required` | Ingest/API keys (comma-separated); requires auth on every route. |
| `TDN_SESSION_SECRET`, `TDN_OIDC_ISSUER`, `TDN_OIDC_CLIENT_ID`, `TDN_OIDC_CLIENT_SECRET`, `TDN_OIDC_REDIRECT_URI`, `TDN_OIDC_ROLE_MAP`, `TDN_OIDC_GROUPS_CLAIM`, `TDN_OIDC_ALLOWED_DOMAINS` | SSO. |
| `TDN_PUBLIC_URL` | External URL used in links sent in alerts. |
| `TDN_JUDGE`, `TDN_JUDGE_MAX_TOKENS` | LLM judge for root-cause analysis; needs a provider key (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`). |
| `TDN_OTLP_FORWARD`, `TDN_OTLP_HEADERS`, `TDN_PRICES`, `TDN_DRIFT_MIN_RUNS`, `TDN_INGEST_RATE_LIMIT`, `TDN_CACHE_TTL`, `TDN_DATABASE_POOL`, `TDN_WORKERS` | Tuning. |

## Scaling notes

- **Partitions vs. workers.** Spans are keyed by `trace_id`, so a whole trace lands on one partition in order. Workers beyond the partition count sit idle, so create the topic with at least as many partitions as `worker.autoscaling.maxReplicas`; the chart's Redpanda defaults to 12.
- **Delivery is at-least-once.** A message is committed only after the pipeline has stored it, and a failed batch is rewound and retried. A redelivery after a crash is harmless because ClickHouse reads deduplicate by span id.
- **ClickHouse routing.** Each project is pinned to a cluster through `routes`, for example to keep EU data on an EU cluster. Reads of a trace with an unknown location fan out to every cluster. `clickhouse.ttlDays` sets span retention.
- **Measured (chdb, the ClickHouse 26.9 engine, on one container, 1M spans):**
  - insert: about 25.6k spans/s, in 20k-span batches;
  - get trace: p50 15 ms, p95 30 ms;
  - tool reliability for one project: p50 9 ms, p95 104 ms;
  - tool reliability over all data: p50 96 ms, p95 142 ms.

  Treat these as a floor for a dedicated server.

## Integration tests

These tests are skipped unless pointed at real services:

```bash
TDN_IT_KAFKA_BOOTSTRAP=127.0.0.1:9092 \
TDN_IT_CLICKHOUSE_URLS=http://127.0.0.1:8123,http://127.0.0.1:8124 \
TDN_IT_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres \
pytest server/tests/integration
python server/tests/integration/load_clickhouse.py --url http://127.0.0.1:8123 --spans 1000000
python server/tests/integration/kafka_throughput.py --bootstrap 127.0.0.1:9092 --database-url postgresql://... --workers 1 3
```

- Without a ClickHouse server, `server/tests/integration/chdb_http.py` serves the embedded ClickHouse engine over HTTP.
- `.github/workflows/integration.yml` runs everything against Apache Kafka 3.9, two ClickHouse 24.8 servers and Postgres 16. It also runs `helm lint`, `helm template` and `kubeconform`.

## Known limits

- **Web API proxy.** The web image bakes in `TDN_API_URL` at build time, because Next.js reads its rewrites then. The ingress therefore routes `/api` to the server directly. To change the target without an ingress, rebuild the image with `--build-arg TDN_API_URL=...`.
- **Bundled data services.** They run a single replica with no backups.
- **`deploy/sql/postgres.sql`.** This file predates the current Postgres store; the store creates its own schema on start (the migration Job).
