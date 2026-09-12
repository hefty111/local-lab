from __future__ import annotations

import hashlib
from pathlib import Path

import xlsxwriter

from bench.report.stats import summarize, RunSummary

MAX_RAW_ROWS = 1_000_000


def build_workbook(runs: dict[str, list[dict]], out_path: Path) -> None:
    wb = xlsxwriter.Workbook(str(out_path))
    bold = wb.add_format({"bold": True})
    pct_fmt = wb.add_format({"num_format": "0.00"})
    red = wb.add_format({"bg_color": "#FFC7CE"})
    green = wb.add_format({"bg_color": "#C6EFCE"})

    labels = list(runs.keys())
    baseline_label = labels[0] if labels else None
    summaries: dict[str, RunSummary] = {label: summarize(rows) for label, rows in runs.items()}

    _write_summary(wb, bold, red, green, labels, baseline_label, summaries)
    _write_by_api_stream(wb, bold, runs)
    _write_timeline(wb, bold, runs)
    _write_failures(wb, bold, runs)
    used_sheet_names: set[str] = set()
    for label, rows in runs.items():
        _write_raw(wb, bold, label, rows, used_sheet_names)

    wb.close()


def _write_summary(wb, bold, red, green, labels, baseline_label, summaries):
    ws = wb.add_worksheet("Summary")
    headers = [
        "run", "requests", "ok_pct", "achieved_rps",
        "latency_p50", "latency_p90", "latency_p99",
        "ttft_p50", "ttft_p90", "ttft_p99",
        "overhead_p50", "overhead_p90", "overhead_p99",
        "sched_lag_p99", "verdict",
    ]
    for col, h in enumerate(headers):
        ws.write(0, col, h, bold)

    baseline = summaries.get(baseline_label) if baseline_label else None
    for row_i, label in enumerate(labels, start=1):
        s = summaries[label]
        verdict = "PASS" if s.ok_pct == 100.0 and s.sched_lag_p99 <= 5.0 else "FAIL"
        values = [
            label, s.requests, s.ok_pct, s.achieved_rps,
            s.latency_p50, s.latency_p90, s.latency_p99,
            s.ttft_p50, s.ttft_p90, s.ttft_p99,
            s.overhead_p50, s.overhead_p90, s.overhead_p99,
            s.sched_lag_p99, verdict,
        ]
        for col, v in enumerate(values):
            ws.write(row_i, col, v)
        fmt = green if verdict == "PASS" else red
        ws.write(row_i, len(headers) - 1, verdict, fmt)


def _write_by_api_stream(wb, bold, runs):
    ws = wb.add_worksheet("By API x Stream")
    headers = ["run", "api", "stream", "requests", "ok_pct", "latency_p50", "latency_p99", "overhead_p50"]
    for col, h in enumerate(headers):
        ws.write(0, col, h, bold)
    row_i = 1
    for label, rows in runs.items():
        buckets: dict[tuple, list[dict]] = {}
        for r in rows:
            key = (r["api"], r["stream"])
            buckets.setdefault(key, []).append(r)
        for (api, stream), bucket_rows in buckets.items():
            s = summarize(bucket_rows)
            ws.write(row_i, 0, label)
            ws.write(row_i, 1, api)
            ws.write(row_i, 2, stream)
            ws.write(row_i, 3, s.requests)
            ws.write(row_i, 4, s.ok_pct)
            ws.write(row_i, 5, s.latency_p50)
            ws.write(row_i, 6, s.latency_p99)
            ws.write(row_i, 7, s.overhead_p50)
            row_i += 1


def _write_timeline(wb, bold, runs):
    ws = wb.add_worksheet("Timeline")
    ws.write(0, 0, "run", bold)
    ws.write(0, 1, "bucket_s", bold)
    ws.write(0, 2, "rps", bold)
    ws.write(0, 3, "p99_latency_ms", bold)
    row_i = 1
    for label, rows in runs.items():
        if not rows:
            continue
        t0 = min(r["t_sent_ms"] for r in rows)
        buckets: dict[int, list[dict]] = {}
        for r in rows:
            bucket = int((r["t_sent_ms"] - t0) // 1000)
            buckets.setdefault(bucket, []).append(r)
        for bucket in sorted(buckets):
            bucket_rows = buckets[bucket]
            s = summarize(bucket_rows)
            ws.write(row_i, 0, label)
            ws.write(row_i, 1, bucket)
            ws.write(row_i, 2, len(bucket_rows))
            ws.write(row_i, 3, s.latency_p99)
            row_i += 1


def _write_failures(wb, bold, runs):
    ws = wb.add_worksheet("Failures")
    headers = ["run", "seed", "reason"]
    for col, h in enumerate(headers):
        ws.write(0, col, h, bold)
    row_i = 1
    for label, rows in runs.items():
        for r in rows:
            if not r["ok"]:
                ws.write(row_i, 0, label)
                ws.write(row_i, 1, r.get("seed", ""))
                ws.write(row_i, 2, r.get("reason", ""))
                row_i += 1


def _raw_sheet_name(label: str, used_names: set[str]) -> str:
    name = f"Raw-{label}"[:31]
    if name not in used_names:
        used_names.add(name)
        return name

    # Collision: disambiguate with a short hash of the full label.
    hash6 = hashlib.blake2b(label.encode(), digest_size=3).hexdigest()
    suffix = f"-{hash6}"
    base = f"Raw-{label}"[: 31 - len(suffix)]
    name = f"{base}{suffix}"
    used_names.add(name)
    return name


def _write_raw(wb, bold, label, rows, used_names):
    ws = wb.add_worksheet(_raw_sheet_name(label, used_names))
    if not rows:
        return
    headers = list(rows[0].keys())
    for col, h in enumerate(headers):
        ws.write(0, col, h, bold)
    for row_i, r in enumerate(rows[:MAX_RAW_ROWS], start=1):
        for col, h in enumerate(headers):
            ws.write(row_i, col, r.get(h))
