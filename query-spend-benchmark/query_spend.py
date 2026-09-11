#!/usr/bin/env python3
"""
Run the "SUM(spend) over a time range" query against LiteLLM_SpendLogs,
either once or N times concurrently, and report timing stats.

Usage:
    # run once
    python query_spend.py

    # run 20 times concurrently
    python query_spend.py --concurrency 20

    # run 100 total queries, 20 at a time
    python query_spend.py --concurrency 20 --total 100

    # custom time range / database
    python query_spend.py \
        --start "2026-08-09 00:00:01.948" \
        --end   "2026-09-20 22:33:16.717" \
        --database-url postgresql://user:pass@host:port/dbname
"""

import argparse
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg2

DEFAULT_DATABASE_URL = "postgresql://llmproxy:dbpassword9090@localhost:6432/litellm"
DEFAULT_START = "2026-08-09 00:00:01.948"
DEFAULT_END = "2026-09-20 22:33:16.717"

QUERY = '''
SELECT SUM(spend) FROM public."LiteLLM_SpendLogs" s
where s."startTime" >= %s
	AND s."startTime" <= %s;
'''


def run_query(database_url: str, start: str, end: str):
    """Open a fresh connection, run the query once, return (result, elapsed_seconds)."""
    t0 = time.perf_counter()
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(QUERY, (start, end))
            result = cur.fetchone()[0]
    finally:
        conn.close()
    elapsed = time.perf_counter() - t0
    return result, elapsed


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--concurrency", type=int, default=1,
        help="Number of queries to run concurrently (default: 1, i.e. run once)",
    )
    parser.add_argument(
        "--total", type=int, default=None,
        help="Total number of queries to run (default: same as --concurrency, i.e. one wave)",
    )
    parser.add_argument("--start", default=DEFAULT_START, help="Range start (startTime >=)")
    parser.add_argument("--end", default=DEFAULT_END, help="Range end (startTime <=)")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        help="Postgres connection string (defaults to $DATABASE_URL or local-setup default)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    total = args.total if args.total is not None else args.concurrency

    if args.concurrency <= 0 or total <= 0:
        print("ERROR: --concurrency and --total must be positive", file=sys.stderr)
        return 1

    print(f"Running {total} quer{'y' if total == 1 else 'ies'} "
          f"with concurrency={args.concurrency} against range "
          f"[{args.start}, {args.end}]...")
    print()

    results = []
    errors = []

    if args.concurrency == 1:
        for i in range(total):
            try:
                result, elapsed = run_query(args.database_url, args.start, args.end)
                results.append((result, elapsed))
                print(f"[{i + 1}/{total}] SUM(spend) = {result}  ({elapsed * 1000:.1f} ms)")
            except Exception as e:
                errors.append(e)
                print(f"[{i + 1}/{total}] ERROR: {e}", file=sys.stderr)
    else:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {
                pool.submit(run_query, args.database_url, args.start, args.end): i
                for i in range(total)
            }
            for future in as_completed(futures):
                i = futures[future]
                try:
                    result, elapsed = future.result()
                    results.append((result, elapsed))
                    print(f"[{i + 1}/{total}] SUM(spend) = {result}  ({elapsed * 1000:.1f} ms)")
                except Exception as e:
                    errors.append(e)
                    print(f"[{i + 1}/{total}] ERROR: {e}", file=sys.stderr)

    print()
    print("--- Summary ---")
    print(f"  Succeeded:  {len(results)}/{total}")
    print(f"  Failed:     {len(errors)}/{total}")

    if results:
        timings = [r[1] for r in results]
        timings_ms = sorted(t * 1000 for t in timings)
        print(f"  Min:        {timings_ms[0]:.1f} ms")
        print(f"  Max:        {timings_ms[-1]:.1f} ms")
        print(f"  Mean:       {statistics.mean(timings_ms):.1f} ms")
        print(f"  Median:     {statistics.median(timings_ms):.1f} ms")
        if len(timings_ms) > 1:
            print(f"  Stdev:      {statistics.stdev(timings_ms):.1f} ms")

        # p95 / p99 (simple nearest-rank)
        def percentile(sorted_values, pct):
            k = max(0, min(len(sorted_values) - 1, int(round(pct / 100 * (len(sorted_values) - 1)))))
            return sorted_values[k]

        print(f"  p95:        {percentile(timings_ms, 95):.1f} ms")
        print(f"  p99:        {percentile(timings_ms, 99):.1f} ms")

        sample_result = results[0][0]
        print(f"  SUM(spend): {sample_result}")

    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
