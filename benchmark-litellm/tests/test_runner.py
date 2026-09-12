import asyncio
import json

import httpx
import pytest

from bench.trace.schema import TraceRequest
from bench.client.runner import run_trace
from mock_llm.server import app as mock_app


def _rows():
    return [
        TraceRequest(i=0, t_ms=0, seed="r0", api="chat", stream=False, model="fake-gpt-4",
                     in_tokens=5, out_tokens=3, duration_ms=10, ttft_ms=None),
        TraceRequest(i=1, t_ms=5, seed="r1", api="chat", stream=True, model="fake-gpt-4",
                     in_tokens=5, out_tokens=4, duration_ms=20, ttft_ms=5),
    ]


@pytest.mark.asyncio
async def test_run_trace_against_mock_all_ok(tmp_path):
    transport = httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as client:
        out_path = tmp_path / "results.jsonl"
        await run_trace(
            rows=_rows(), client=client, base_url="http://mock", api_key="k",
            api_mode="chat", target_label="baseline", time_scale=0.01,
            max_inflight=10, out_path=out_path,
        )
    lines = out_path.read_text().strip().splitlines()
    assert len(lines) == 2
    records = [json.loads(l) for l in lines]
    assert all(r["ok"] for r in records)
    assert {r["i"] for r in records} == {0, 1}
    # sched_lag_ms compares t_sent_ms (absolute monotonic-ms) against t_sched_ms;
    # both must live in the same reference frame or this blows up to a huge number.
    for r in records:
        assert abs(r["sched_lag_ms"]) < 5000
