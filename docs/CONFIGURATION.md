# Configuration

Every setting is an environment variable prefixed `TDN_`. The same variables work for `tracedown-server`, `tracedown-worker`, Docker Compose and the Helm chart (see [DEPLOYMENT.md](DEPLOYMENT.md) for how the chart sets them).

| Variable | Purpose |
|---|---|
| `TDN_DB` | SQLite path for metadata (single-process setups) |
| `TDN_DATABASE_URL` | Postgres for metadata, e.g. `postgresql://user:pass@host/tracedown` (`TDN_DATABASE_POOL` connections per process) |
| `TDN_REDIS_URL` | Redis: shared read cache, live feed across replicas, ingest rate limits (`TDN_CACHE_TTL`, `TDN_INGEST_RATE_LIMIT`) |
| `TDN_API_KEYS` | Bootstrap owner keys (comma-separated). Without keys, users or this, the server runs open (dev) |
| `TDN_AUTH=required` | Refuse open mode even on a fresh install |
| `TDN_JUDGE` | Optional LLM judge, e.g. `anthropic:claude-haiku-5-5` or `openai:gpt-4.1-mini` (uses `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`) |
| `TDN_OIDC_ISSUER`, `_CLIENT_ID`, `_CLIENT_SECRET`, `_REDIRECT_URI`, `_ROLE_MAP`, `_ALLOWED_DOMAINS` | Single sign-on |
| `TDN_SESSION_SECRET` | Signs SSO session cookies (set it in production) |
| `TDN_CLICKHOUSE` | ClickHouse URL, or JSON router config for multi-cluster |
| `TDN_KAFKA_BOOTSTRAP` | Ingest through Kafka/Redpanda; run `tracedown-worker`. The topic is created if missing (`TDN_KAFKA_PARTITIONS`, default 12) |
| `TDN_OTLP_FORWARD`, `TDN_OTLP_HEADERS` | Forward finished runs to an OTLP endpoint |
| `TDN_PRICES` | Extra or corrected model prices, USD per million tokens, as JSON or a path to a JSON file: `{"model": {"input": .., "output": ..}}`. Current Anthropic, OpenAI and Google models are priced out of the box ([sources](https://github.com/nbn07/agent-tracedown/blob/main/server/tracedown_server/pricing.py)) |
| `TDN_DRIFT_MIN_RUNS` | Runs per cohort before a drift report (default 20) |
| `TDN_PUBLIC_URL` | Used in notification links and the cookie `secure` flag |



## Command-line options

`tracedown-server --help`, `tracedown-worker --help` and `tracedown-import --help` list the flags.
