from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import httpx

from bench.client import openai_chat, anthropic_messages, sse, verify
from bench.client.results import RequestResult, ResultsWriter
from bench.client.scheduler import run_schedule
from bench.trace.schema import TraceRequest


def _builder_for(api: str):
    return openai_chat.build_request if api == "chat" else anthropic_messages.build_request


def _parser_for(api: str):
    return sse.parse_openai_chat_stream if api == "chat" else sse.parse_anthropic_messages_stream


async def _execute_one(
    row: TraceRequest,
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    api_mode: str,
    target_label: str,
    t_sched_ms: float,
    writer: ResultsWriter,
    default_timeout_s: float,
) -> None:
    api = api_mode or row.api
    build_request = _builder_for(api)
    parse_stream = _parser_for(api)
    url, headers, body = build_request(row, base_url=base_url, api_key=api_key)

    # t_sched_ms and t_sent must live in the same reference frame (absolute
    # monotonic-clock milliseconds) so that sched_lag_ms = t_sent - t_sched_ms
    # is a small, meaningful value rather than a huge nonsensical number.
    t_sent = time.monotonic() * 1000.0

    status = 0
    ok = False
    reason: Optional[str] = None
    content = ""
    completion_tokens = None
    terminated = False
    chunk_count = 0
    t_first_byte = None
    t_first_token = None
    body_bytes = 0

    timeout = httpx.Timeout(row.duration_ms / 1000.0 * 3 + default_timeout_s)

    try:
        if row.stream:
            async with client.stream("POST", url, headers=headers, json=body, timeout=timeout) as resp:
                status = resp.status_code
                lines = []
                async for line in resp.aiter_lines():
                    now_ms = time.monotonic() * 1000.0
                    if t_first_byte is None:
                        t_first_byte = now_ms
                    if line.strip().startswith("data:") and t_first_token is None:
                        t_first_token = now_ms
                    lines.append(line)
                    body_bytes += len(line)
                result = parse_stream(lines)
                content, completion_tokens = result.content, result.completion_tokens
                terminated, chunk_count = result.terminated, result.chunk_count
        else:
            resp = await client.post(url, headers=headers, json=body, timeout=timeout)
            status = resp.status_code
            now_ms = time.monotonic() * 1000.0
            t_first_byte = now_ms
            t_first_token = now_ms
            data = resp.json()
            body_bytes = len(resp.content)
            if api == "chat":
                content = data["choices"][0]["message"]["content"]
                completion_tokens = data.get("usage", {}).get("completion_tokens")
            else:
                content = "".join(b.get("text", "") for b in data.get("content", []))
                completion_tokens = data.get("usage", {}).get("output_tokens")
            terminated = True
            chunk_count = 1

        vr = verify.verify_response(
            expected_seed=row.seed, expected_out_tokens=row.out_tokens,
            got_content=content, got_completion_tokens=completion_tokens,
            stream_terminated=terminated, http_status=status,
        )
        ok, reason = vr.ok, vr.reason
    except Exception as exc:  # noqa: BLE001 - benchmark must never crash on one bad request
        reason = "timeout" if isinstance(exc, httpx.TimeoutException) else "connect"
        status = status or 0

    t_done = time.monotonic() * 1000.0
    writer.write(RequestResult(
        i=row.i, seed=row.seed, api=api, stream=row.stream, target=target_label,
        t_sched_ms=t_sched_ms, t_sent_ms=t_sent,
        t_first_byte_ms=t_first_byte if t_first_byte is not None else t_done,
        t_first_token_ms=t_first_token if t_first_token is not None else t_done,
        t_done_ms=t_done, status=status, ok=ok, reason=reason,
        chunks=chunk_count, bytes=body_bytes, out_tokens_seen=completion_tokens,
        duration_ms=row.duration_ms,
    ))


async def run_trace(
    rows: list[TraceRequest],
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    api_mode: Optional[str],  # None => use row.api; "chat"/"messages" => force
    target_label: str,
    time_scale: float,
    max_inflight: int,
    out_path: Path,
    default_timeout_s: float = 10.0,
) -> None:
    writer = ResultsWriter(out_path)
    offsets_ms = [r.t_ms for r in rows]
    by_index = {r.i: r for r in rows}

    # Reference point (absolute monotonic ms) matching run_schedule's own
    # internal `start = time.monotonic()`, so that t_sched_ms below lives in
    # the same absolute reference frame as t_sent (captured in _execute_one
    # via `time.monotonic() * 1000.0`). Without this, sched_lag_ms would mix
    # an absolute timestamp with a small schedule-relative offset.
    t0_ms = time.monotonic() * 1000.0

    async def callback(i: int) -> None:
        row = by_index[i]
        await _execute_one(
            row=row, client=client, base_url=base_url, api_key=api_key,
            api_mode=api_mode, target_label=target_label,
            t_sched_ms=t0_ms + row.t_ms * time_scale, writer=writer,
            default_timeout_s=default_timeout_s,
        )

    try:
        await run_schedule(offsets_ms, time_scale=time_scale, callback=callback, max_inflight=max_inflight)
    finally:
        writer.close()
