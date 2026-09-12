# Why `SUM(spend)` over `LiteLLM_SpendLogs` is slow — and why concurrency makes it *drastically* slower

Companion to `BENCHMARK_REPORT.md`. Everything below was re-measured on the same
stack (postgres:16 in docker, pgbouncer in front, 8 vCPU / 12 GB WSL2 VM). All
numbers are from `EXPLAIN (ANALYZE, BUFFERS)` with `track_io_timing=on` and from
`pg_stat_activity` sampled *during* the 20-wide run.

## TL;DR

1. **Single query:** the query is a full heap scan of **5.6 GB / 705 k pages**
   that is almost entirely **CPU-bound** (tuple deforming + filter + `read()`
   syscall copying from OS page cache), not disk-bound. Postgres runs with
   stock defaults (`shared_buffers = 128 MB`, i.e. **16 384 pages** — 2.3 % of
   the heap), so every run is a 5.6 GB copy from the kernel page cache into a
   ring of shared buffers. ~2.2 s with 2 parallel workers, ~4 s serial, plus
   ~0.5 s of LLVM JIT compilation per run. The 6.3 s in the report is that plus
   a partly-cold page cache and a fresh connection through pgbouncer.

2. **20 concurrent:** three things stack:
   - **CPU saturation.** Each scan needs ~4 CPU-seconds. 20 scans = ~80 CPU-s on
     8 cores ⇒ ≥10 s even with perfect scheduling. Measured: postgres pinned
     at **~680 % CPU**, `%iowait = 0`.
   - **Loss of parallelism.** `max_parallel_workers = 8` cluster-wide. The
     first ~4 queries grab 2 workers each; the other 16 silently run **serial**
     (the plan still *says* "Workers Planned: 2" but "Launched: 0"). That is
     why min ≈ 14.5 s (got workers) and median ≈ 31 s (did not).
   - **Lock contention on the tiny buffer pool.** 20–27 processes are
     simultaneously evicting and inserting into a 16 384-slot cache while
     streaming ~14 million page reads through it. The dominant wait event
     sampled was **`LWLock:BufferMapping`** (up to 19 of 27 processes at once),
     plus **30–40 % system CPU** burned in `read()`/`pread()` copying pages the
     buffer pool can't hold.

   Net: 6.3 s → 31 s (≈5×), and under sustained load queued waves inherit the
   same saturation, so tail latency keeps growing (p99 40 s).

---

## Part 1 — Why a single query takes seconds

### 1.1 The "37 GB" is misleading; the scan reads 5.6 GB

```
heap (main fork)   5 630 MB
TOAST              29 GB      <- never touched by this query
indexes            2 465 MB
total              37 GB
```

`LiteLLM_SpendLogs` stores big JSON columns (`metadata`, `messages`,
`response`, `proxy_server_request`, …) which live out-of-line in TOAST.
`SUM(spend)` only needs `startTime` and `spend`, both inline, so the sequential
scan reads **only the heap**: `relpages ≈ 720 000` → **705 051 pages read +
15 572 hit = 720 623 pages ≈ 5.6 GB** per execution. Roughly **~500 bytes of
heap per row** for a query that needs 16 bytes/row. The scan is paying to
deform every tuple to extract two columns.

### 1.2 The planner is right to ignore the index

The range covers **11 355 000 of 11 356 563 rows (99.99 %)**. An index range
scan would touch every index leaf page *and* every heap page in random order;
a seq scan touches every heap page once, sequentially. Cost estimate:
seq 791 600 vs. the index alternative being far higher. This is not a
missing-index problem. Even the `(startTime, request_id)` composite index does
not help because `spend` is not in it (no index-only scan is possible) — and
with only 3.5 % of the heap ever visible to the visibility map anyway an IOS
would still heap-fetch.

### 1.3 Where the ~2–4 seconds actually go

Measured on a warm OS page cache:

| Configuration                    | Execution time | `shared read` I/O time | Notes                          |
|----------------------------------|---------------:|-----------------------:|--------------------------------|
| 2 parallel workers, JIT on       | 2 245 ms       | 2 895 ms (3 procs)     | plan from the report           |
| 2 parallel workers, JIT off      | 2 225 ms       | 2 405 ms (3 procs)     |                                |
| serial (0 workers), JIT on       | 3 980 ms       | 1 777 ms               | JIT: 111 ms                    |
| serial (0 workers), JIT off      | 4 426 ms       | 1 747 ms               |                                |

Reading this:

- **`shared hit=15 572 read=705 051`** on every run. Hit ratio **2.2 %**. The
  cumulative `pg_statio_user_tables` hit ratio for this table is **3.49 %**.
  `shared_buffers` is 128 MB (16 384 × 8 kB) — the postgres:16 image default,
  and nothing in `docker-compose.yml` overrides it. A 5.6 GB scan through a
  128 MB cache **can never hit**; Postgres also deliberately uses a 256 kB
  ring buffer for large seq scans so it won't even try to cache it.
- **`I/O Timings: shared read ≈ 1.75 s`** for a serial scan even though the
  data is in the kernel page cache (`buff/cache ≈ 9.7 GB`, `%iowait = 0`).
  That time is **~705 k `pread()` syscalls** each copying 8 kB kernel → user.
  This is the ~40 % of the query that is "I/O" and it is pure memory copy +
  syscall overhead, not disk. (`effective_io_concurrency = 1`, no read-ahead
  hint; PG16 has no async I/O for heap scans.)
- **The remaining ~2.2 s serial is CPU**: tuple deforming of ~500-byte rows to
  get at two columns, evaluating two timestamp comparisons on 11.36 M rows,
  and summing. ~200 ns/row — normal for PG.
- **JIT costs ~0.1–0.5 s per execution** (`Inlining 340 ms` in the parallel
  plan because each worker compiles independently). With `jit=on` (default)
  and cost 804 430 > `jit_above_cost` (100 000), every run pays LLVM
  compilation. It buys back almost nothing here (2 245 vs 2 225 ms).
- **Parallelism helps ~1.8×** (3 980 → 2 245 ms) because the work is CPU-bound
  and splits cleanly. `max_parallel_workers_per_gather = 2` (default) caps it
  at 3 processes.

### 1.4 Why the report saw 6.3 s and not 2.2 s

- Page cache was partly cold (the table is 37 GB total; RAM is 12 GB; the
  litellm proxy and loader had been writing). Cold pages come from a Hyper-V
  virtual disk (`sdd`, `rotational=1`).
- `time.perf_counter()` in `query_spend.py` wraps `psycopg2.connect()` — a
  fresh TCP + SCRAM handshake to pgbouncer, then pgbouncer → postgres.
- JIT compile (~0.5 s) plus process fork for 2 parallel workers.

When the page cache is fully warm the honest floor is ~2.2 s; there is nothing
below that without changing the query shape or the data layout.

---

## Part 2 — Why 20 concurrent queries go from 6 s to ~30 s

Naive expectation: 20 independent scans of cached data on 8 cores should
finish in ~2.5–3× single latency. Measured: **~5× (median 31 s)**, with a
bimodal distribution (min 14.5 s, median 31 s). Three mechanisms explain it.

### 2.1 Hard CPU saturation — 0 % iowait

Sampled while the wave was running:

```
%Cpu(s): 54.0 us, 39.5 sy, 6.5 id, 0.0 wa      <- top, whole VM
litellm-local-postgres-1   676.75 %            <- docker stats (8 cores = 800 %)
```

Serial scan cost ≈ 4 CPU-s (from §1.3). 20 scans ≈ **80 CPU-seconds** of
mandatory work. On 8 cores that is **≥ 10 s wall even with zero overhead**.
Parallel workers do not add capacity here — they just split the same CPU-s
across more processes competing for the same 8 cores. Anything that further
inflates CPU-per-page (next two sections) is multiplied by 20.

### 2.2 Cluster-wide parallel worker exhaustion → most queries silently go serial

```
max_worker_processes           = 8
max_parallel_workers           = 8    <- total for the whole cluster
max_parallel_workers_per_gather= 2
```

Sampled `pg_stat_activity`:

```
sample 1:  parallel workers = 7   active client backends = 21
sample 2:  parallel workers = 1   active client backends = 18
sample 3:  parallel workers = 0   active client backends = 17
```

Only **4 queries** can hold 2 workers at once. The other 16 plan a
`Gather … Workers Planned: 2` but launch **0** workers and execute the whole
scan in the leader process (Postgres does not re-plan; it degrades silently).
Those queries are the 4 s-CPU serial variant, not the 2.2 s parallel one.
This is exactly the **bimodal latency**: the ~4 lucky queries finish around
**14 s**, the rest cluster at **28–31 s**.

Note that the planner's cost estimate assumed parallelism; nothing tells the
client that it did not get it.

### 2.3 `LWLock:BufferMapping` — 20 scanners thrashing a 16 384-slot cache

Sampled wait events for the benchmark's backends:

```
sample 1:
  client backend  | LWLock | BufferMapping | 14
  parallel worker | LWLock | BufferMapping |  5
  client backend  | IO     | DataFileRead  |  4
  (running, no wait)                        |  5
sample 2:
  client backend  | LWLock | BufferMapping | 11
  parallel worker | LWLock | BufferMapping |  3
  client backend  | IO     | DataFileRead  |  2
  (running)                                 |  6
```

At peak **19 of 27 postgres processes were blocked on `BufferMapping`**.

What that lock is: the shared-buffer lookup table is a hash partitioned into
128 `BufferMapping` LWLocks. Every page read that misses shared buffers has
to (a) pick a victim via the clock sweep, (b) take the partition lock for the
*victim's* tag to delete it, (c) take the partition lock for the *new* tag to
insert it — in exclusive mode. With `shared_buffers = 16 384` pages and each
backend streaming 705 k page misses, the wave performs **~14 million
evict-and-insert operations** through a 16 k-slot table. Each backend
thrashes the same tiny pool the others are using, so the buffer just evicted
by backend A is often the one backend B was about to reuse — and every one of
those operations serializes on a partition lock. Single-query this costs
almost nothing (one process, no contention); at 20+ processes it becomes the
top wait event.

This is not about disk: `%iowait` was 0 and `DataFileRead` waits were a
minority. It is lock convoying on the buffer manager, and it is a direct
consequence of the 128 MB default.

### 2.4 System time explosion (30–40 % `sy`)

`top` showed **26–40 % of all 8 cores in kernel mode**. That is 20 backends
each issuing ~705 k `pread(8 kB)` calls (~14 M syscalls, ~112 GB of
kernel→user memcpy for one wave) plus futex/spin from the LWLock contention
above. Single-query the same copying happened but on one core with no
contention; now it competes with the useful work for the same 8 cores.

### 2.5 Why `synchronize_seqscans` doesn't save it

Postgres *does* try to make concurrent seq scans on the same relation start
where the current leader is so they share buffer reads
(`synchronize_seqscans = on`). It helps a little (the hit count rose from
~15.5 k to slightly more) but it only shares the *read into shared buffers*.
Every backend still has to deform and filter every tuple itself (the CPU
cost), the leader-follower group drifts apart as soon as scheduling is uneven
(which it is at 27 processes on 8 cores), and with a 256 kB ring per scan the
window in which a follower can hit a leader's page is tiny. Parallel workers
also split the table by block range and don't follow the sync point.

### 2.6 Why sustained load (100 @ 20) has a worse tail

The system is already at 100 % CPU with a 20-deep wave. `pgbouncer` has
`DEFAULT_POOL_SIZE = 20` and `POOL_MODE = transaction`, so exactly 20 server
connections exist; the 21st–100th client simply waits in pgbouncer's queue
for a server slot. Each query's measured latency (`psycopg2.connect()` → fetch)
therefore includes **queueing in pgbouncer + a full ~30 s scan**. Waves don't
overlap cleanly (finishers are replaced one by one), so the parallel-worker
lottery reshuffles constantly and the unlucky serial queries at the end of a
wave-boundary stack up: p99 = 40.6 s, stdev doubles (5.7 → 11.1 s).

---

## Part 3 — Putting numbers to it

| Component                           | Single (warm)       | 20-wide, per query                       |
|-------------------------------------|---------------------|------------------------------------------|
| Pages read from OS                  | 705 k (5.6 GB)      | 705 k each, 14 M total                   |
| Processes doing the work            | 3 (leader + 2)      | 1 for 16 queries, 3 for ~4 queries       |
| Available cores per query           | ~3 of 8             | 8 / 20–27 ≈ 0.3–0.4                       |
| CPU-seconds needed                  | ~6 (split 3 ways)   | ~4 serial, ×20 = 80 total                |
| Theoretical wall at 100 % CPU       | ~2.2 s              | ≥ 10 s                                   |
| BufferMapping contention            | none                | 19/27 processes blocked at peak          |
| Kernel time overhead                | ~1.7 s on 1 core    | 30–40 % of all 8 cores                   |
| **Measured**                        | **2.2 s** (6.3 cold)| **14.5 s min / 31 s median**             |

The gap between ≥10 s (pure CPU math) and 31 s (measured) is the
`BufferMapping` convoy and the syscall/memcpy tax — both artefacts of
running a 5.6 GB streaming scan through a 128 MB buffer pool, 20× over.

---

## Part 4 — What would actually change the picture (root causes, not tuning)

Listed by leverage, with the mechanism from above each one removes.

1. **Don't scan the heap for this question at all.**
   The query is "sum of spend for a date range" over a table where each row is
   ~500 B of heap + ~2.5 kB of TOAST but the answer needs 16 B/row. LiteLLM
   already has `LiteLLM_DailyUserSpend` / daily aggregate tables for exactly
   this; the equivalent day-bucketed query reads a few thousand rows. Failing
   that, a `MATERIALIZED VIEW` / summary table bucketed by hour with
   `SUM(spend)` reduces the scan by 4–5 orders of magnitude. This removes §1.1,
   §1.3, §2.1–2.4 entirely.

2. **Covering index for an index-only scan** —
   `CREATE INDEX … ON "LiteLLM_SpendLogs" ("startTime") INCLUDE (spend)`
   + keep the table vacuumed so the visibility map is set. The scan becomes
   ~300–400 MB of index leaf pages instead of 5.6 GB of heap and does no tuple
   deforming. ~15× less data, and it fits in a sane `shared_buffers`.
   (Note: today `last_vacuum`/`autovacuum` show the table was analyzed but the
   VM is likely sparse after a bulk load; run `VACUUM` first or the IOS will
   heap-fetch.)

3. **Fix the buffer pool** — `shared_buffers` ≥ 2–4 GB on this 12 GB box
   (and `effective_cache_size` to match). This does not make a single 5.6 GB
   scan cached, but it kills the `BufferMapping` convoy (§2.3) because 20
   scans no longer fight over 16 k slots, and lets the covering index from (2)
   live entirely in shared memory.

4. **Stop lying about parallelism under load** — either raise
   `max_parallel_workers`/`max_worker_processes` so 20 concurrent queries can
   each get workers (bounded by cores: on 8 vCPUs there is *no* real capacity,
   so this mostly just makes latency uniform rather than faster), or set
   `max_parallel_workers_per_gather = 0` for this workload so every query is
   equally serial and the bimodal 14 s/31 s split disappears. The honest fix
   for §2.1 is fewer scans, not more workers.

5. **Turn off JIT for this query shape** (`jit = off` or raise
   `jit_above_cost`): saves ~0.1–0.5 s per execution for zero loss (§1.3),
   and under concurrency that is 20 × LLVM compilations competing for the
   same saturated cores.

6. **Bound concurrency at the proxy, not in pgbouncer.** `DEFAULT_POOL_SIZE = 20`
   with a CPU-bound 4 s query means the DB is committed to ~80 CPU-s per wave
   no matter what. A pool of 4–6 for analytical queries would make each one
   finish in ~3–4 s and queue the rest — same throughput, far better p50, and
   the queueing becomes visible in pgbouncer stats instead of hidden inside
   lock waits.

Anything else (tuning `random_page_cost`, adding more `startTime` indexes,
increasing `work_mem`) does not touch any mechanism identified above.

---

## Reproduction of the evidence

```bash
PSQL="docker exec litellm-local-postgres-1 psql -U llmproxy -d litellm -Atc"

# sizes: heap vs toast vs indexes
$PSQL "select pg_size_pretty(pg_relation_size('\"LiteLLM_SpendLogs\"')) heap,
              pg_size_pretty(pg_indexes_size('\"LiteLLM_SpendLogs\"')) idx,
              pg_size_pretty(pg_total_relation_size('\"LiteLLM_SpendLogs\"')) total"

# settings that matter
$PSQL "select name, setting, unit from pg_settings where name in
 ('shared_buffers','max_parallel_workers','max_parallel_workers_per_gather',
  'max_worker_processes','jit','effective_io_concurrency')"

# per-run buffer + I/O accounting (warm cache)
$PSQL "set track_io_timing=on; explain (analyze, buffers)
 SELECT SUM(spend) FROM public.\"LiteLLM_SpendLogs\" s
 WHERE s.\"startTime\" >= '2026-08-09 00:00:01.948'
   AND s.\"startTime\" <= '2026-09-20 22:33:16.717'"

# what backends are waiting on during the 20-wide wave (run in a 2nd shell)
cd local-setup/query-spend-benchmark && .venv/bin/python query_spend.py --concurrency 20 &
sleep 6
$PSQL "select backend_type, wait_event_type, wait_event, count(*)
       from pg_stat_activity where query ilike '%SUM(spend)%'
       group by 1,2,3 order by 4 desc"
docker stats --no-stream litellm-local-postgres-1
top -bn1 | head -4
```
