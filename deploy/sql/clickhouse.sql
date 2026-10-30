-- Tracedown: ClickHouse schema (standard + scale profiles).
-- Append-only, high-volume span storage. Ordering by (project, trace, time) makes
-- "load one run" a single range scan; partition TTL enforces retention.

CREATE TABLE IF NOT EXISTS spans
(
    project_id      LowCardinality(String),
    trace_id        FixedString(32),
    span_id         FixedString(16),
    parent_span_id  String,
    name            String,
    kind            LowCardinality(String),      -- agent | llm | tool | retrieval | memory | decision | handoff | embedding
    step            UInt32,
    agent_name      LowCardinality(String),
    start_time      DateTime64(9, 'UTC'),
    end_time        DateTime64(9, 'UTC'),
    duration_ms     Float64 MATERIALIZED dateDiff('microsecond', start_time, end_time) / 1000,
    status_code     LowCardinality(String),      -- UNSET | OK | ERROR
    status_message  String,
    model           LowCardinality(String),
    tool_name       LowCardinality(String),
    input_tokens    UInt32,
    output_tokens   UInt32,
    attributes      Map(String, String),
    input_digest    String,
    output_digest   String
)
ENGINE = MergeTree
PARTITION BY toDate(start_time)
ORDER BY (project_id, trace_id, start_time)
TTL toDate(start_time) + INTERVAL 30 DAY
SETTINGS index_granularity = 8192;

ALTER TABLE spans ADD INDEX IF NOT EXISTS idx_tool tool_name TYPE set(1000) GRANULARITY 4;
ALTER TABLE spans ADD INDEX IF NOT EXISTS idx_status status_code TYPE set(3) GRANULARITY 4;

-- Hourly rollups for dashboards: success rate, tokens, tool reliability.
CREATE TABLE IF NOT EXISTS span_rollup_hourly
(
    project_id   LowCardinality(String),
    agent_name   LowCardinality(String),
    kind         LowCardinality(String),
    name         String,
    hour         DateTime('UTC'),
    calls        UInt64,
    errors       UInt64,
    input_tokens UInt64,
    output_tokens UInt64,
    duration_ms_sum Float64
)
ENGINE = SummingMergeTree
PARTITION BY toYYYYMM(hour)
ORDER BY (project_id, agent_name, kind, name, hour);

CREATE MATERIALIZED VIEW IF NOT EXISTS span_rollup_hourly_mv TO span_rollup_hourly AS
SELECT project_id, agent_name, kind, name, toStartOfHour(start_time) AS hour,
       count() AS calls, countIf(status_code = 'ERROR') AS errors,
       sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
       sum(duration_ms) AS duration_ms_sum
FROM spans
GROUP BY project_id, agent_name, kind, name, hour;
