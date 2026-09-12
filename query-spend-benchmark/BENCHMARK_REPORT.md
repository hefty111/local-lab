# Query Spend Benchmark Report

Date: 2026-09-12

## What was tested

The `query_spend.py` script runs this query against `LiteLLM_SpendLogs`:

```sql
SELECT SUM(spend) FROM public."LiteLLM_SpendLogs" s
WHERE s."startTime" >= %s
  AND s."startTime" <= %s;
```

against a local docker-compose stack (`litellm-local`) with:
- pgbouncer on `localhost:6432` in front of
- postgres:16 on `localhost:5432`

Time range used: `2026-08-09 00:00:01.948` to `2026-09-20 22:33:16.717` (covers essentially the whole table).

### Table stats

- Row count: **11,355,000**
- Total relation size: **37 GB**
- Indexes present: `pkey (request_id)`, `startTime`, `end_user`, `session_id`, `(startTime, request_id)`

### Query plan

```
Finalize Aggregate  (cost=804430.43..804430.44 rows=1 width=8)
  ->  Gather  (cost=804430.22..804430.43 rows=2 width=8)
        Workers Planned: 2
        ->  Partial Aggregate  (cost=803430.22..803430.23 rows=1 width=8)
              ->  Parallel Seq Scan on "LiteLLM_SpendLogs" s  (cost=0.00..791600.61 rows=4731841 width=8)
                    Filter: (("startTime" >= ...) AND ("startTime" <= ...))
```

Even though `startTime` is indexed, Postgres chooses a **parallel sequential scan** because the selected range covers nearly all 11.3M rows — an index scan would not be selective enough to be cheaper than a seq scan. This means every query in the benchmark reads essentially the entire 37 GB table.

## Results

### 1. Single query (baseline, no concurrency)

| Metric | Value |
|---|---|
| Result (SUM(spend)) | 48526.62 |
| Latency | **6,317.7 ms** |

A single cold-ish query over the full table takes ~6.3 seconds.

### 2. One wave of 20 concurrent queries (`--concurrency 20`)

| Metric | Value (ms) |
|---|---|
| Min | 13,719.6 |
| Max | 28,896.8 |
| Mean | 25,792.0 |
| Median | 28,548.6 |
| Stdev | 5,706.5 |
| p95 | 28,894.5 |
| p99 | 28,896.8 |

Running 20 identical full-table-scan queries at once roughly **4–5x's** the single-query latency (6.3s → ~26-29s median), since they contend for CPU/IO/buffer cache and there are only 2 parallel workers planned per query.

### 3. 100 total queries, 20 in flight at a time (`--concurrency 20 --total 100`)

| Metric | Value (ms) |
|---|---|
| Min | 11,212.4 |
| Max | 40,655.8 |
| Mean | 27,956.7 |
| Median | 28,923.6 |
| Stdev | 11,080.6 |
| p95 | 39,649.8 |
| p99 | 40,590.9 |

Sustained load (100 queries, 20-wide) shows similar median latency to the single wave, but **tail latency grows notably worse** (p99 ~40.6s vs 28.9s for one wave), and variance increases (stdev ~11.1s vs 5.7s). This indicates the database is saturated at concurrency=20 for this query shape — the system has no more headroom, so extra waves queue up behind in-flight scans rather than running in parallel.

# How to reproduce

```bash
cd local-setup/query-spend-benchmark

# single query
.venv/bin/python query_spend.py --database-url "postgresql://llmproxy:dbpassword9090@localhost:6432/litellm"

# one wave of 20 concurrent queries
.venv/bin/python query_spend.py --concurrency 20 --database-url "postgresql://llmproxy:dbpassword9090@localhost:6432/litellm"

# 100 total queries, 20 at a time
.venv/bin/python query_spend.py --concurrency 20 --total 100 --database-url "postgresql://llmproxy:dbpassword9090@localhost:6432/litellm"
```
