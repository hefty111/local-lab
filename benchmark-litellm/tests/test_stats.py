from bench.report.stats import summarize, RunSummary


def _records():
    return [
        {"api": "chat", "stream": True, "ok": True, "reason": None,
         "latency_ms": 100 + i, "ttft_ms": 10 + i, "overhead_ms": 5,
         "sched_lag_ms": 1, "t_sent_ms": i * 10, "t_done_ms": i * 10 + 100 + i}
        for i in range(10)
    ] + [
        {"api": "chat", "stream": True, "ok": False, "reason": "content_mismatch",
         "latency_ms": 500, "ttft_ms": 50, "overhead_ms": 400, "sched_lag_ms": 2,
         "t_sent_ms": 1000, "t_done_ms": 1500}
    ]


def test_summarize_basic_fields():
    s = summarize(_records())
    assert isinstance(s, RunSummary)
    assert s.requests == 11
    assert round(s.ok_pct, 2) == round(10 / 11 * 100, 2)
    assert s.errors["content_mismatch"] == 1
    assert s.latency_p50 <= s.latency_p90 <= s.latency_p99


def test_summarize_empty_records_does_not_crash():
    s = summarize([])
    assert s.requests == 0
    assert s.ok_pct == 0.0
