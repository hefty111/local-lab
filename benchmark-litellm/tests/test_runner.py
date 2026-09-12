import asyncio
import json

import httpx
import pytest

from bench.detgen import text as detgen_text
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


@pytest.mark.asyncio
async def test_run_trace_dispatches_per_row_api_when_mode_none(tmp_path):
    """With api_mode=None, run_trace must dispatch each row to the builder
    matching *that row's* api field, not a single global mode. We record the
    requested URL path per call via a MockTransport to prove the chat row
    hits /v1/chat/completions and the messages row hits /v1/messages."""
    seen_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={
                "choices": [{"message": {"content": detgen_text("r0", 3)}}],
                "usage": {"completion_tokens": 3},
            })
        else:
            return httpx.Response(200, json={
                "content": [{"type": "text", "text": detgen_text("r1", 4)}],
                "usage": {"output_tokens": 4},
            })

    transport = httpx.MockTransport(handler)
    rows = [
        TraceRequest(i=0, t_ms=0, seed="r0", api="chat", stream=False, model="fake-gpt-4",
                     in_tokens=5, out_tokens=3, duration_ms=10, ttft_ms=None),
        TraceRequest(i=1, t_ms=5, seed="r1", api="messages", stream=False, model="fake-claude",
                     in_tokens=5, out_tokens=4, duration_ms=10, ttft_ms=None),
    ]
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as client:
        out_path = tmp_path / "results.jsonl"
        await run_trace(
            rows=rows, client=client, base_url="http://mock", api_key="k",
            api_mode=None, target_label="baseline", time_scale=0.01,
            max_inflight=10, out_path=out_path,
        )
    records = [json.loads(l) for l in out_path.read_text().strip().splitlines()]
    by_i = {r["i"]: r for r in records}
    assert by_i[0]["api"] == "chat"
    assert by_i[1]["api"] == "messages"
    assert by_i[0]["ok"] is True
    assert by_i[1]["ok"] is True
    assert "/v1/chat/completions" in seen_paths
    assert "/v1/messages" in seen_paths


@pytest.mark.asyncio
async def test_run_trace_parses_anthropic_non_streaming_response(tmp_path):
    """Exercise the Anthropic-shaped response-parsing branch specifically:
    content is joined from data['content'][*]['text'] and completion_tokens
    comes from data['usage']['output_tokens']."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": detgen_text("seedA", 6)}],
            "usage": {"output_tokens": 6},
        })

    transport = httpx.MockTransport(handler)
    rows = [
        TraceRequest(i=0, t_ms=0, seed="seedA", api="messages", stream=False, model="fake-claude",
                     in_tokens=5, out_tokens=6, duration_ms=10, ttft_ms=None),
    ]
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as client:
        out_path = tmp_path / "results.jsonl"
        await run_trace(
            rows=rows, client=client, base_url="http://mock", api_key="k",
            api_mode="messages", target_label="baseline", time_scale=0.01,
            max_inflight=10, out_path=out_path,
        )
    records = [json.loads(l) for l in out_path.read_text().strip().splitlines()]
    assert len(records) == 1
    r = records[0]
    assert r["ok"] is True
    assert r["out_tokens_seen"] == 6


@pytest.mark.asyncio
async def test_run_trace_content_mismatch_marks_not_ok(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "this is not the expected deterministic text"}}],
            "usage": {"completion_tokens": 3},
        })

    transport = httpx.MockTransport(handler)
    rows = [
        TraceRequest(i=0, t_ms=0, seed="r0", api="chat", stream=False, model="fake-gpt-4",
                     in_tokens=5, out_tokens=3, duration_ms=10, ttft_ms=None),
    ]
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as client:
        out_path = tmp_path / "results.jsonl"
        await run_trace(
            rows=rows, client=client, base_url="http://mock", api_key="k",
            api_mode="chat", target_label="baseline", time_scale=0.01,
            max_inflight=10, out_path=out_path,
        )
    records = [json.loads(l) for l in out_path.read_text().strip().splitlines()]
    assert len(records) == 1
    assert records[0]["ok"] is False
    assert records[0]["reason"] == "content_mismatch"


@pytest.mark.asyncio
async def test_run_trace_timeout_marks_not_ok(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated")

    transport = httpx.MockTransport(handler)
    rows = [
        TraceRequest(i=0, t_ms=0, seed="r0", api="chat", stream=False, model="fake-gpt-4",
                     in_tokens=5, out_tokens=3, duration_ms=10, ttft_ms=None),
    ]
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as client:
        out_path = tmp_path / "results.jsonl"
        await run_trace(
            rows=rows, client=client, base_url="http://mock", api_key="k",
            api_mode="chat", target_label="baseline", time_scale=0.01,
            max_inflight=10, out_path=out_path,
        )
    records = [json.loads(l) for l in out_path.read_text().strip().splitlines()]
    assert len(records) == 1
    assert records[0]["ok"] is False
    assert records[0]["reason"] == "timeout"


@pytest.mark.asyncio
async def test_run_trace_connect_error_marks_not_ok(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated")

    transport = httpx.MockTransport(handler)
    rows = [
        TraceRequest(i=0, t_ms=0, seed="r0", api="chat", stream=False, model="fake-gpt-4",
                     in_tokens=5, out_tokens=3, duration_ms=10, ttft_ms=None),
    ]
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as client:
        out_path = tmp_path / "results.jsonl"
        await run_trace(
            rows=rows, client=client, base_url="http://mock", api_key="k",
            api_mode="chat", target_label="baseline", time_scale=0.01,
            max_inflight=10, out_path=out_path,
        )
    records = [json.loads(l) for l in out_path.read_text().strip().splitlines()]
    assert len(records) == 1
    assert records[0]["ok"] is False
    assert records[0]["reason"] == "connect"
