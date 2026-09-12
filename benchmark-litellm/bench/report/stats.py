from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class RunSummary:
    requests: int
    ok_pct: float
    errors: Counter
    achieved_rps: float
    latency_p50: float
    latency_p90: float
    latency_p99: float
    latency_max: float
    ttft_p50: float
    ttft_p90: float
    ttft_p99: float
    overhead_p50: float
    overhead_p90: float
    overhead_p99: float
    sched_lag_p99: float


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    return float(np.percentile(values, q))


def summarize(records: list[dict]) -> RunSummary:
    n = len(records)
    if n == 0:
        return RunSummary(
            requests=0, ok_pct=0.0, errors=Counter(), achieved_rps=0.0,
            latency_p50=0.0, latency_p90=0.0, latency_p99=0.0, latency_max=0.0,
            ttft_p50=0.0, ttft_p90=0.0, ttft_p99=0.0,
            overhead_p50=0.0, overhead_p90=0.0, overhead_p99=0.0, sched_lag_p99=0.0,
        )

    ok_count = sum(1 for r in records if r["ok"])
    errors = Counter(r["reason"] for r in records if not r["ok"] and r.get("reason"))

    latency = [r["latency_ms"] for r in records]
    ttft = [r["ttft_ms"] for r in records]
    overhead = [r["overhead_ms"] for r in records]
    sched_lag = [r["sched_lag_ms"] for r in records]

    span_ms = max(r["t_done_ms"] for r in records) - min(r["t_sent_ms"] for r in records)
    achieved_rps = n / (span_ms / 1000.0) if span_ms > 0 else 0.0

    return RunSummary(
        requests=n,
        ok_pct=ok_count / n * 100.0,
        errors=errors,
        achieved_rps=achieved_rps,
        latency_p50=_pct(latency, 50), latency_p90=_pct(latency, 90),
        latency_p99=_pct(latency, 99), latency_max=max(latency),
        ttft_p50=_pct(ttft, 50), ttft_p90=_pct(ttft, 90), ttft_p99=_pct(ttft, 99),
        overhead_p50=_pct(overhead, 50), overhead_p90=_pct(overhead, 90),
        overhead_p99=_pct(overhead, 99),
        sched_lag_p99=_pct(sched_lag, 99),
    )
