from datetime import datetime, timedelta

from bench.trace.from_spendlogs import row_to_trace, should_sample


def _row(**overrides):
    base = dict(
        request_id="req-1",
        startTime=datetime(2026, 1, 1, 0, 0, 0),
        endTime=datetime(2026, 1, 1, 0, 0, 2, 300000),
        completionStartTime=datetime(2026, 1, 1, 0, 0, 0, 400000),
        prompt_tokens=120,
        completion_tokens=40,
        call_type="acompletion",
        model="gpt-4o",
    )
    base.update(overrides)
    return base


def test_row_to_trace_basic_mapping():
    first_start = datetime(2026, 1, 1, 0, 0, 0)
    row = _row()
    tr = row_to_trace(row, first_start=first_start, i=0, model_map={"gpt-4o": "fake-gpt-4"})
    assert tr is not None
    assert tr.t_ms == 0
    assert tr.duration_ms == 2300
    assert tr.ttft_ms == 400
    assert tr.stream is True
    assert tr.api == "chat"
    assert tr.model == "fake-gpt-4"
    assert tr.in_tokens == 120
    assert tr.out_tokens == 40


def test_row_to_trace_anthropic_call_type():
    first_start = datetime(2026, 1, 1, 0, 0, 0)
    row = _row(call_type="anthropic_messages", completionStartTime=None)
    tr = row_to_trace(row, first_start=first_start, i=1, model_map={})
    assert tr.api == "messages"
    assert tr.stream is False
    assert tr.ttft_ms is None


def test_row_to_trace_drops_zero_tokens():
    row = _row(completion_tokens=0)
    tr = row_to_trace(row, first_start=datetime(2026, 1, 1), i=0, model_map={})
    assert tr is None


def test_row_to_trace_drops_nonpositive_duration():
    row = _row(endTime=datetime(2026, 1, 1, 0, 0, 0))  # equal to startTime
    tr = row_to_trace(row, first_start=datetime(2026, 1, 1), i=0, model_map={})
    assert tr is None


def test_should_sample_deterministic():
    a = should_sample("req-123", 0.5)
    b = should_sample("req-123", 0.5)
    assert a == b


def test_should_sample_full_and_zero():
    assert should_sample("any-id", 1.0) is True
    assert should_sample("any-id", 0.0) is False
