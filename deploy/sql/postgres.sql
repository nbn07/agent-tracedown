-- Tracedown: Postgres schema (standard + scale profiles).
-- Holds what users create and edit. High-volume spans live in ClickHouse (clickhouse.sql).

CREATE TABLE IF NOT EXISTS projects (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name             TEXT NOT NULL UNIQUE,
    capture_content  BOOLEAN NOT NULL DEFAULT TRUE,   -- store prompts/completions as blobs
    retention_days   INTEGER NOT NULL DEFAULT 30,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS api_keys (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    key_hash    TEXT NOT NULL UNIQUE,                 -- sha256 of the key; the key itself is shown once
    scopes      TEXT[] NOT NULL DEFAULT ARRAY['ingest', 'read'],
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at  TIMESTAMPTZ
);

-- One row per run, for listing and filtering. Written by the span-writer worker when the root span lands.
CREATE TABLE IF NOT EXISTS runs (
    trace_id        CHAR(32) PRIMARY KEY,
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    agent_name      TEXT NOT NULL,
    outcome         TEXT NOT NULL CHECK (outcome IN ('success', 'failure', 'unknown')),
    error           TEXT,
    started_at      TIMESTAMPTZ NOT NULL,
    duration_ms     DOUBLE PRECISION,
    span_count      INTEGER,
    input_tokens    BIGINT DEFAULT 0,
    output_tokens   BIGINT DEFAULT 0,
    cost_usd        NUMERIC(12, 6),
    models          TEXT[],
    prompt_version  TEXT,
    replay_of       CHAR(32),
    tags            JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS runs_project_time ON runs (project_id, started_at DESC);
CREATE INDEX IF NOT EXISTS runs_project_outcome ON runs (project_id, outcome, started_at DESC);
CREATE INDEX IF NOT EXISTS runs_tags ON runs USING GIN (tags);

-- RCA output: one row per finding, so findings can be queried across runs.
CREATE TABLE IF NOT EXISTS findings (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id    CHAR(32) NOT NULL REFERENCES runs(trace_id) ON DELETE CASCADE,
    rank        SMALLINT NOT NULL,
    category    TEXT NOT NULL,
    title       TEXT NOT NULL,
    span_id     CHAR(16) NOT NULL,
    step        INTEGER NOT NULL,
    confidence  REAL NOT NULL,
    is_symptom  BOOLEAN NOT NULL DEFAULT FALSE,
    is_root     BOOLEAN NOT NULL DEFAULT FALSE,
    caused_by   UUID REFERENCES findings(id),
    detector    TEXT NOT NULL,                       -- detector name, or 'llm_judge'
    evidence    JSONB NOT NULL DEFAULT '[]',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS findings_trace ON findings (trace_id, rank);
CREATE INDEX IF NOT EXISTS findings_category ON findings (category, created_at DESC);

-- Runs grouped by a shared root-cause signature: (category, agent, tool or source, normalized error).
CREATE TABLE IF NOT EXISTS incidents (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id     UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    signature      TEXT NOT NULL,
    title          TEXT NOT NULL,
    root_cause     TEXT NOT NULL,
    suggested_fix  TEXT,
    run_count      INTEGER NOT NULL DEFAULT 0,
    first_seen     TIMESTAMPTZ NOT NULL,
    last_seen      TIMESTAMPTZ NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged', 'resolved')),
    UNIQUE (project_id, signature)
);

CREATE TABLE IF NOT EXISTS replays (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_trace_id  CHAR(32) NOT NULL REFERENCES runs(trace_id) ON DELETE CASCADE,
    replay_trace_id  CHAR(32),
    mode             TEXT NOT NULL CHECK (mode IN ('exact', 'from-step', 'swap', 'full')),
    from_step        INTEGER,
    overrides        JSONB NOT NULL DEFAULT '{}',
    status           TEXT NOT NULL DEFAULT 'planned',
    diff             JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
