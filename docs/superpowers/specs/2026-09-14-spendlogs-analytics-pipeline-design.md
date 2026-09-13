# LiteLLM Spend Logs Analytics Pipeline — Design

Date: 2026-09-14
Status: approved design, pending implementation plan

## 1. Goal

Provide an interactive BI dashboard (Grafana) over LiteLLM spend data that:

- answers totals, per-dimension breakdowns, and hourly time series for any user-selected time range (hourly boundaries);
- stays responsive with at least 50 concurrent dashboard users;
- scales to ~50M spend logs over a 14-day retention window (~3.5M rows/day, ~150k rows/hour);
- exposes *enriched* rows: fields flattened from JSONB, joins against LiteLLM tables, external lookups (e.g. cost center), redaction of secrets, and filtering of uninteresting rows;
- tolerates minutes of ingest lag;
- requires no changes to the `stack-litellm` compose project, the LiteLLM proxy, or its Prisma-managed schema.

Enrichment is applied once at ingest time. Later changes to lookup sources affect only newly ingested rows.

## 2. Problem

`LiteLLM_SpendLogs` is an OLTP table with five JSONB columns. Measured in `benchmark-spendlogs-olap/`:

- 11.36M rows = 37 GB (heap 5.6 GB, TOAST 29 GB, indexes 2.5 GB); ~500 bytes of heap read per row for a query that needs ~16.
- `SUM(spend)` over the full range: 6.3 s single query, 28.5 s median at 20 concurrent, p99 40.6 s at 100 queries / 20 in flight. Postgres saturates at 20 concurrent (`LWLock:BufferMapping`).
- A compressed TimescaleDB hypertable of the same data ran the same query in 142 ms single / 1.4 s median at 20 concurrent, but converting the raw table requires a PK change (Prisma drift) and a 65-minute compression rewrite of JSONB the query never reads.

Independently of storage, the enrichment requirements (external lookups, filtering) can only be implemented in application code, not in SQL triggers or continuous aggregates.

## 3. Approaches considered

| Approach | Verdict |
|---|---|
| Covering index + Postgres tuning on the raw table | Rejected: ~15x less I/O but still scans ~1.5 GB per 14-day query; saturates at 50 concurrent. |
| Same-DB hourly rollup refreshed by polling | Viable for un-enriched data; cannot host external lookups. |
| Trigger on `LiteLLM_SpendLogs` into a same-DB Timescale hypertable | Viable; couples the LiteLLM write path to our table and cannot host external lookups. |
| Convert `LiteLLM_SpendLogs` itself into a hypertable | Rejected: Prisma schema drift, 45 GB compression rewrite. |
| ClickHouse sidecar fed by ETL | Viable; more than the stated query shapes need (YAGNI). |
| Custom LiteLLM logger -> RabbitMQ -> sink -> separate Timescale DB | Viable; sub-second freshness and a reusable event bus, but callbacks are fire-and-forget (loss on proxy crash / broker outage without an outbox), payload is `StandardLoggingPayload` rather than the SpendLogs row, and adds three stateful components. Freshness advantage is not required. |
| Logical replication into a staging table, service drains it | Viable; exactly-once from WAL but needs `wal_level = logical` and a replication slot on the production DB, and still needs the transform service. |
| **Watermark poller -> transform service -> separate Timescale DB (chosen)** | No LiteLLM code, trigger, or broker; exactly-once through an idempotent loader; trivially restartable and backfillable; the source is an adapter, so a RabbitMQ or logical-replication source can be added later without touching transform or loader. |

Facts from LiteLLM v1.98.0 source that the chosen design relies on:

- `LiteLLM_SpendLogs` is append-only. The only runtime write is `create_many(skip_duplicates=True)` -> `INSERT ... ON CONFLICT DO NOTHING`, at most 1000 rows / 2 MB per statement, flushed every ~2-15 s (`litellm/proxy/utils.py:6224`). There is no UPDATE path.
- The `metadata` JSONB already contains `user_api_key_team_alias`, `user_api_key_alias`, `user_api_key_user_email`, `user_api_key_org_id`, so most "joins" can be served from the row itself.
- Retention of the raw table (LiteLLM's `SpendLogCleanup`) deletes in 1000-row batches and is independent of this design.

## 4. Architecture

```
stack-litellm (existing, unchanged)          stack-analytics (new compose project, joins litellm-net)
+------------------------------+             +--------------------------------------------------+
| litellm -> postgres:16       |             |  spendlogs-etl (Python, 1 container)              |
|   "LiteLLM_SpendLogs"        |<-- poll ----|    source: WatermarkPoller                        |
|   LiteLLM_TeamTable, etc.    |<-- joins ---|    transform: flatten, join, lookup, redact, filter|
+------------------------------+             |    loader: batch upsert                          |
                                             |        |                                          |
                                             |        v                                          |
                                             |  analytics-db (timescale/timescaledb-ha:pg16)     |
                                             |    spend_events   hypertable, 1d chunks,          |
                                             |                   compress >1d, drop >14d         |
                                             |    spend_hourly   continuous aggregate            |
                                             |    etl_state, etl_dead_letter                     |
                                             +---------------------+----------------------------+
                                                                   |
stack-observability (existing)                                     v
   grafana -- datasource "analytics-db" -- dashboard "LiteLLM Spend BI" (queries spend_hourly)
```

Only two new containers. The source DB is read through the existing PgBouncer (`pgbouncer:5432` on `litellm-net`).

### Repository layout

```
stack-analytics/
  docker-compose.yml                  # analytics-db, spendlogs-etl
  .env.example
  analytics-db/init/01_schema.sql     # idempotent: extension, tables, hypertable, cagg, policies
  etl/
    pyproject.toml
    Dockerfile
    spendlogs_etl/
      config.py                       # env-driven settings
      source.py                       # WatermarkPoller
      transform/
        __init__.py                   # Step protocol, Pipeline runner
        flatten.py
        join_litellm.py
        lookup_external.py
        redact.py
        filter.py
      lookups.py                      # TTL caches; LiteLLM table lookups; external provider interface
      loader.py                       # batch INSERT ... ON CONFLICT DO NOTHING
      metrics.py                      # Prometheus exporter
      main.py                         # loop
    tests/
stack-observability/grafana/provisioning/datasources/datasources.yml   # + analytics-db
stack-observability/grafana/dashboards/litellm-spend-bi.json           # new
benchmark-spendlogs-olap/query_spend.py                                # + --sink mode
```

## 5. Data model (analytics-db)

### 5.1 `spend_events` (hypertable)

One row per spend log that survived filtering (~150 bytes).

| Group | Columns |
|---|---|
| Identity / time | `request_id TEXT`, `start_time TIMESTAMPTZ`, `end_time TIMESTAMPTZ`, `ingested_at TIMESTAMPTZ DEFAULT now()` |
| Copied from SpendLogs | `call_type TEXT`, `model TEXT`, `model_group TEXT`, `custom_llm_provider TEXT`, `team_id TEXT`, `user_id TEXT`, `end_user TEXT`, `status TEXT`, `cache_hit BOOLEAN`, `spend NUMERIC(14,8)`, `prompt_tokens INT`, `completion_tokens INT`, `total_tokens INT`, `request_duration_ms INT` |
| Flattened from `metadata` / `request_tags` | `team_alias TEXT`, `key_alias TEXT`, `user_email TEXT`, `org_id TEXT`, `tags TEXT[]` |
| Lookup-derived (initial set; extend with `ALTER TABLE ADD COLUMN` + a transform step) | `cost_center TEXT`, `department TEXT`, `model_family TEXT` |
| Redacted | `api_key_hash TEXT` (first 16 hex chars of sha256). Raw `api_key`, `messages`, `response`, `proxy_server_request`, `metadata` are never stored. |

- `SELECT create_hypertable('spend_events', by_range('start_time', INTERVAL '1 day'))`.
- Primary key `(request_id, start_time)`; index `(start_time, team_id)`.
- Compression: `segmentby = 'team_id, model'`, `orderby = 'start_time DESC'`; policy compresses chunks older than 1 day.
- Retention policy: drop chunks older than 14 days.
- Loader writes with `ON CONFLICT (request_id, start_time) DO NOTHING`; redelivery is a no-op.

### 5.2 `spend_hourly` (continuous aggregate)

```sql
CREATE MATERIALIZED VIEW spend_hourly
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket('1 hour', start_time) AS bucket,
       team_id, team_alias, model, custom_llm_provider, call_type, status, cost_center,
       count(*)               AS requests,
       sum(spend)             AS spend,
       sum(prompt_tokens)     AS prompt_tokens,
       sum(completion_tokens) AS completion_tokens,
       sum(total_tokens)      AS total_tokens
FROM spend_events
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8;
```

- Refresh policy: every 5 minutes, window `start_offset = 3 hours`, `end_offset = 5 minutes`.
- `materialized_only = false`: the current hour is computed live from the uncompressed chunk.
- Retention on the aggregate: 14 days, matching raw rows.
- Dimension set is deliberately narrower than `spend_events` to bound cardinality. `api_key_hash`, `user_id`, `end_user` are excluded; breakdowns by them query `spend_events` directly.

### 5.3 Operational tables

- `etl_state(key TEXT PRIMARY KEY, value JSONB NOT NULL, updated_at TIMESTAMPTZ)` with rows `watermark` (last `startTime` fully processed, ISO timestamp) and `last_run` (rows read/loaded/filtered/dead-lettered, duration, error).
- `etl_dead_letter(id BIGSERIAL PRIMARY KEY, request_id TEXT, failed_at TIMESTAMPTZ, step TEXT, error TEXT, raw JSONB)`.

## 6. ETL service (`spendlogs-etl`)

### 6.1 Main loop

```
every POLL_INTERVAL_S (default 30):
  wm = etl_state.watermark                      (first run: now() - 14 days)
  cursor = SELECT <needed columns incl. metadata, request_tags>
           FROM "LiteLLM_SpendLogs"
           WHERE "startTime" >  wm - LATENESS_S   (default 180)
             AND "startTime" <= now() - SAFETY_S  (default 30)
           ORDER BY "startTime"                   (server-side cursor, BATCH_SIZE rows at a time, default 5000)
  for each batch:
    events = pipeline.run(rows)                   # per-row; None = filtered; exceptions -> dead letter
    in one sink transaction:
      loader.upsert(events)                       # multi-row INSERT ... ON CONFLICT DO NOTHING, <= 1000 rows/stmt
      write dead-letter rows
      etl_state.watermark = max(startTime) in batch
      etl_state.last_run  = metrics
```

- The lateness window re-reads ~3 minutes of rows every cycle; the idempotent loader makes this harmless. It absorbs LiteLLM's batch flush delay (2 s poll, <= ~15 s flush, retries with backoff).
- `SAFETY_S` avoids reading a range LiteLLM may still be committing into.
- Backfill or replay: set `etl_state.watermark` to any earlier time.
- The source query uses the existing `LiteLLM_SpendLogs_startTime_idx`; steady state reads ~150k rows/hour, all cache-hot.

### 6.2 Transform pipeline

`Step` protocol: `(Event) -> Event | None`. `Pipeline` holds an ordered list of steps read from config and applies them per row. Steps are pure functions with table-driven unit tests; only the runner knows the order.

1. `flatten` — project SpendLogs columns; extract `metadata.user_api_key_team_alias`, `user_api_key_alias`, `user_api_key_user_email`, `user_api_key_org_id`; normalise `request_tags` to `TEXT[]`; parse `cache_hit` string to boolean.
2. `join_litellm` — fill `team_alias`, `key_alias`, `user_email` from `metadata` first; fall back to `LiteLLM_TeamTable`, `LiteLLM_VerificationToken`, `LiteLLM_UserTable` via bulk-loaded, TTL-cached maps (default TTL 300 s). Lookups are done once per batch for the distinct keys, never per row.
3. `lookup_external` — resolve `cost_center`, `department`, `model_family` through a `LookupProvider` interface. First implementation: YAML file mapping (`LOOKUP_FILE`) keyed by `team_id` / `model`. Cached with TTL. On miss or provider error the value is `'unknown'` and a counter increments; ingest never blocks.
4. `redact` — `api_key_hash = sha256(api_key)[:16]`; drop `api_key`, `metadata`, and any field not in the `spend_events` schema.
5. `filter` — drop rows matching configured rules (`FILTER_RULES`, JSON list of `{field, op, value}` with `op` in `eq, neq, in, not_in`). Default rule set shipped in `.env.example`: `[{"field": "model", "op": "eq", "value": "health-check"}]`. An empty list disables filtering.

### 6.3 Configuration (environment)

`SOURCE_DSN` (via PgBouncer), `SINK_DSN`, `POLL_INTERVAL_S`, `LATENESS_S`, `SAFETY_S`, `BATCH_SIZE`, `LOOKUP_TTL_S`, `LOOKUP_FILE`, `FILTER_RULES`, `METRICS_PORT` (default 9108), `LOG_LEVEL`.

### 6.4 Error handling

| Failure | Behaviour |
|---|---|
| Source DB unreachable | Log, exponential backoff up to 60 s, retry. Watermark unchanged. |
| Sink failure mid-batch | Transaction rolls back; watermark not advanced; next cycle re-reads the same rows. No partial state. |
| Per-row transform exception | Row written to `etl_dead_letter` with step and error; counter increments; batch continues. |
| Lookup provider miss or error | Field set to `'unknown'`; counter increments. |
| Process crash | On restart, resumes from the persisted watermark minus lateness. |

### 6.5 Observability

Prometheus endpoint `/metrics`: `etl_rows_read_total`, `etl_rows_loaded_total`, `etl_rows_filtered_total`, `etl_rows_dead_lettered_total`, `etl_lag_seconds` (`now() - watermark`), `etl_batch_duration_seconds`, `etl_lookup_cache_hits_total` / `misses_total`, `etl_lookup_provider_errors_total`. Prometheus in `stack-observability` scrapes it.

Grafana "ETL health" row: lag (alert > 600 s), dead-letter rate, batch duration. Reconciliation panel: hourly `count(*)` from `LiteLLM_SpendLogs` for the trailing 6 hours (index range scan, cheap) against `spend_hourly.requests + filtered count` for the same hours.

## 7. Dashboard

- Provisioned datasource `analytics-db` (PostgreSQL type, `timescaledb: true`) pointing directly at the analytics container. `max_connections = 200` on that Postgres; no pooler needed for one Grafana instance.
- Provisioned dashboard `LiteLLM Spend BI`. All panels query `spend_hourly` with `$__timeFilter(bucket)`. Template variables `team`, `model`, `provider`, `cost_center` from `SELECT DISTINCT` on the aggregate.
- Panels: total spend, requests, tokens (stat); spend over time using `$__timeGroup(bucket, $__interval)`; top-N by team, model, cost center, provider (bar); status breakdown; ETL health row (section 6.5).
- The existing "Postgres — Source of Truth Spend" panels in `litellm-sre-command-center.json` are re-pointed from `LiteLLM_SpendLogs` to `spend_hourly`.

## 8. Testing

- **Unit**: each transform step table-driven from fixtures derived from the mock generator's "golden" row; pipeline runner ordering and dead-letter behaviour; loader SQL generation; watermark arithmetic.
- **Integration** (pytest, docker): source Postgres seeded with the mock generator + analytics-db. Assert: row counts match minus filtered; re-running with a rewound watermark produces no duplicates; a row inserted with an old `startTime` after the first pass is picked up within the lateness window; a poisoned row lands in `etl_dead_letter` and the batch completes.
- **Performance**: `benchmark-spendlogs-olap/query_spend.py --sink` runs the dashboard's real queries against `spend_hourly` for 14-day and 1-day ranges. Acceptance: p95 < 500 ms at 50 concurrent. ETL steady-state lag < 120 s while the mock generator writes 3.5M rows/day.

## 9. Rollout

1. `docker compose up` in `stack-analytics/`; init SQL creates schema and policies.
2. ETL starts with watermark `now() - 14d` and backfills existing rows (11M rows at 5k/batch, estimated 10-20 minutes; bounded by JSONB TOAST reads on the source).
3. Provision Grafana datasource and dashboard; confirm the reconciliation panel agrees with the source.
4. Re-point the SRE dashboard's raw-scan panels to `spend_hourly`.

## 10. Out of scope

- Any change to `stack-litellm`, LiteLLM callbacks, or triggers on Prisma-managed tables.
- Drill-down to raw rows, latency percentiles, tag-based filtering. All are later additions on `spend_events` without redesign.
- Retention of the raw `LiteLLM_SpendLogs` table (handled by LiteLLM's `maximum_spend_logs_retention_period` or its partitioning scripts).
- Retroactive re-enrichment when lookup sources change.
- Alternative source adapters (RabbitMQ, logical replication). The `source.py` interface is the extension point.
