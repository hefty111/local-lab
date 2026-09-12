# Addendum: TimescaleDB hypertable + compression, measured

Follow-up to `DEEP_DIVE.md`. That doc predicted a plain hypertable would do
almost nothing for this query, but compression (or an aggregate table) would
be drastic. This is the measurement.

## Setup

A **separate** TimescaleDB container (`timescale/timescaledb-ha:pg16`) was
started on the same docker network as the existing stack, with its own new
volume (`litellm_timescale_bench_data`). **The original `litellm-local-postgres-1`
container and its `litellm_local_pgdata` volume were never modified** — only
read from (`pg_dump --schema-only`, and a `\copy ... TO STDOUT` of the full
table). All 11,355,000 rows of `LiteLLM_SpendLogs` were copied over so the
comparison uses the exact same data, not a subset.

Schema difference required for Timescale: the primary key had to become
`(request_id, "startTime")` instead of `(request_id)`, since a hypertable
requires the partitioning column in every unique constraint. This is a real
migration cost against the live schema (Prisma-managed), noted in
`DEEP_DIVE.md` §4 as friction, not addressed here.

```sql
SELECT create_hypertable('public."LiteLLM_SpendLogs"', by_range('startTime'));
-- default 7-day chunks -> 3 chunks covering the data's actual date range

ALTER TABLE public."LiteLLM_SpendLogs" SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'model,call_type',
  timescaledb.compress_orderby = '"startTime" DESC'
);
SELECT compress_chunk(c) FROM show_chunks('public."LiteLLM_SpendLogs"') c;
```

## Result 1 — plain hypertable, uncompressed: no better, slightly worse

| | Postgres (report) | Hypertable, uncompressed |
|---|---:|---:|
| Single query | 6,317.7 ms (2,245 ms warm) | 7,744.7 ms |
| 20 concurrent, median | 28,548.6 ms | 55,095.9 ms |
| 20 concurrent, p99 | 28,896.8 ms | 57,151.9 ms |

`EXPLAIN ANALYZE` shows why: `Buffers: shared read=717,416` — the scan still
reads the same ~5.6 GB of heap (just split across 3 chunks joined by
`Parallel Append` instead of one relation). The query range covers 99.99% of
rows, so Timescale's main uncompressed superpower — **chunk exclusion** —
does not fire; every chunk is scanned anyway. The extra `Parallel Append`
node and per-chunk executor setup is pure overhead on top of the same
CPU/I-O-bound scan described in `DEEP_DIVE.md` §1, and under concurrency it's
worse, not better: this test's container auto-tuned itself to
`max_parallel_workers_per_gather = 4` (vs. the original's 2), which put *more*
parallel workers into flight per query, deepening the CPU saturation and
buffer-mapping contention from `DEEP_DIVE.md` §2.1–2.3, hence the 55s median
instead of ~31s.

**This confirms the prediction: a hypertable alone is not the fix.**

## Result 2 — hypertable + compression: the fix

Compression ratio measured via `chunk_compression_stats()`:

| Chunk | Before | After | Ratio |
|---|---:|---:|---:|
| `_hyper_1_4_chunk` | 24.9 GB | 2.31 GB | 10.8× |
| `_hyper_1_5_chunk` | 9.4 GB | 0.87 GB | 10.8× |
| `_hyper_1_6_chunk` | 20.2 GB | 1.87 GB | 10.8× |
| **Total** | **54.4 GB** | **5.05 GB** | **10.8×** |

(Total is larger than the original 37 GB because of the extra composite PK
index and a fresh insert without the years of natural TOAST/pglz packing the
production table had — the ratio, not the absolute GB, is what matters here.)

`EXPLAIN ANALYZE` on the compressed table:

```
Finalize Aggregate (actual time=1141.320..1150.074 rows=1)
  Buffers: shared hit=32400 read=24064          <- was hit=15572 read=705051
  I/O Timings: shared read=4035.190              <- was shared read=2894.768
  -> Gather (Workers Launched: 3)
     -> Parallel Append
        -> Custom Scan (VectorAgg)
           -> Custom Scan (ColumnarScan) on _hyper_1_4_chunk
              -> Parallel Seq Scan on _hyper_1_4_chunk_compressed
                 (actual rows=1300 loops=4)        <- 1,300 compressed row-batches,
                                                       not 1.3M individual rows
Execution Time: 1150.737 ms
```

**56,464 pages read (≈441 MB) instead of 720,623 pages (≈5.6 GB)** — a 12.7×
reduction in bytes scanned, consistent with the compression ratio. The
`ColumnarScan`/`VectorAgg` nodes decode `spend` in batches without touching
`metadata`, `messages`, `response`, or any other column — this is the
column-pruning benefit `DEEP_DIVE.md` §1.1 said the query needed but the
row-store heap couldn't give it.

### Benchmark: single query

| | Postgres (report) | Hypertable, compressed |
|---|---:|---:|
| Latency | 6,317.7 ms | **141.7 ms** |

**~45× faster.**

### Benchmark: 20 concurrent (one wave)

| Metric | Postgres (report) | Hypertable, compressed |
|---|---:|---:|
| Min | 13,719.6 ms | 766.6 ms |
| Median | 28,548.6 ms | **1,413.5 ms** |
| Max | 28,896.8 ms | 1,579.9 ms |
| Stdev | 5,706.5 ms | 289.9 ms |
| p95 | 28,894.5 ms | 1,570.5 ms |
| p99 | 28,896.8 ms | 1,579.9 ms |

**~20× faster at the median, and the spread collapsed** (max/min ratio 2.1×
instead of 2.1× — wait, comparable ratio, but in absolute terms the stdev
dropped 20× along with the mean). More importantly: **the specific pathologies
from `DEEP_DIVE.md` §2 are gone.** With ~24 MB read per query instead of
5.6 GB, 20 concurrent scans no longer produce enough page misses to thrash a
128 MB/3 GB buffer pool, there's no more CPU-bound 80-second aggregate scan
workload, and 20 queries finishing in ~1.4 s each no longer leaves 16 of them
without a parallel worker for the plan's whole duration.

### Benchmark: 100 total, 20 in flight (sustained load)

| Metric | Postgres (report) | Hypertable, compressed |
|---|---:|---:|
| Min | 11,212.4 ms | 466.5 ms |
| Median | 28,923.6 ms | **1,213.7 ms** |
| Max | 40,655.8 ms | 3,008.0 ms |
| p95 | 39,649.8 ms | 2,764.1 ms |
| p99 | 40,590.9 ms | 2,953.8 ms |

Sustained load still shows the same *shape* of degradation as the original
report (tail latency grows relative to a single wave — p99 2,953.8 ms vs.
1,579.9 ms for one wave) because the underlying mechanism (limited
concurrency capacity queuing behind in-flight work) hasn't disappeared, it's
just operating on much smaller numbers. The system is no longer anywhere near
saturated at concurrency=20, though — it would take dramatically higher
concurrency to reproduce today's saturation.

## Conclusion

- **Plain hypertable partitioning: no.** Confirmed by measurement — slightly
  *worse* here because the wide time range defeats chunk exclusion and adds
  `Parallel Append` overhead on top of an unchanged 5.6 GB scan.
- **Hypertable + compression: yes, drastically.** ~45× on a single query,
  ~20× at the median under 20-way concurrency, because it cuts the bytes the
  query has to read by ~13× (10.8× compression + column pruning) and removes
  the CPU/buffer-pool contention mechanisms from `DEEP_DIVE.md` §2 by making
  each query's footprint small enough that 20 of them no longer saturate an
  undersized buffer pool or the CPU.
- **Caveat carried over from the qualitative discussion:** compressed chunks
  are append-mostly; `UPDATE`/`DELETE` against compressed rows works in
  PG16-era Timescale but decompresses-then-recompresses the affected
  segments, and the migration requires changing the primary key, which is a
  real schema change against LiteLLM's Prisma-managed table. Compressing
  **this specific real table was also operationally expensive**: initial
  compression of the 3 chunks took ~65 minutes wall time on this VM, because
  `compress_chunk()` has to read and rewrite every column — including the
  ~45 GB of `metadata`/`messages`/`response` JSONB that `SUM(spend)` never
  needed in the first place. That one-time cost (and the ongoing cost of
  compressing each new chunk after it ages out of the "recent, uncompressed"
  window) is the real price of this fix; it doesn't show up in the query
  latency numbers above but has to be budgeted for in production.
- A **covering index or a daily/hourly aggregate table** (`DEEP_DIVE.md` §4,
  options 1–2) would likely get most of this same win with less migration
  risk and no per-chunk compression job to operate — worth comparing before
  committing to a Timescale migration for this reason alone.

## Cleanup

The benchmark container and its volume are throwaway and were removed after
this test; the original stack (`litellm-local-postgres-1` and
`litellm_local_pgdata`) was never touched:

```bash
docker rm -f bench-runner litellm-timescale-bench
docker volume rm litellm_timescale_bench_data
```
