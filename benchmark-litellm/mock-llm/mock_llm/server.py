"""Mock OpenAI-compatible LLM server with deterministic, per-request
controllable output (content, token count, TTFT, total duration, jitter).

Control is read from a top-level `mock` object in the request body
(forwarded by LiteLLM's `extra_body`), with a fallback marker embedded in
the first user message: `[mock seed=... out=... dur=... ttft=... jitter=...]`
This marker doubles as a cache-buster for LiteLLM's Redis response cache.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
import uuid
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from bench.detgen import text

app = FastAPI(title="Mock LLM Server v2")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

_STATE = {"inflight": 0, "total": 0, "errors": 0}

MARKER_RE = re.compile(
    r"\[mock seed=(?P<seed>\S+) out=(?P<out>\d+) dur=(?P<dur>\d+)"
    r"(?: ttft=(?P<ttft>\d+))?(?: jitter=(?P<jitter>[\d.]+))?\]"
)


def _parse_mock_control(body: dict) -> dict:
    mock = body.get("mock")
    if isinstance(mock, dict) and "seed" in mock and "out_tokens" in mock:
        return {
            "seed": str(mock["seed"]),
            "out_tokens": int(mock["out_tokens"]),
            "duration_ms": int(mock.get("duration_ms", 1000)),
            "ttft_ms": mock.get("ttft_ms"),
            "jitter": float(mock.get("jitter", 0.2)),
        }

    messages = body.get("messages") or []
    first_user = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
    if isinstance(first_user, list):
        first_user = " ".join(
            part.get("text", "") for part in first_user if isinstance(part, dict)
        )
    match = MARKER_RE.search(first_user or "")
    if match:
        gd = match.groupdict()
        return {
            "seed": gd["seed"],
            "out_tokens": int(gd["out"]),
            "duration_ms": int(gd["dur"]),
            "ttft_ms": int(gd["ttft"]) if gd["ttft"] else None,
            "jitter": float(gd["jitter"]) if gd["jitter"] else 0.2,
        }

    # No control found: deterministic default so the server never 500s.
    fallback_seed = str(uuid.uuid4())
    return {"seed": fallback_seed, "out_tokens": 8, "duration_ms": 50, "ttft_ms": None, "jitter": 0.0}


def _gaps_ms(n_chunks: int, ttft_ms: int, duration_ms: int, jitter: float) -> list[float]:
    """Return n_chunks-1 inter-chunk gaps (after the first token) summing to
    duration_ms - ttft_ms, with per-gap multiplicative jitter, renormalised
    so the total is exact."""
    remaining = max(0, duration_ms - ttft_ms)
    n_gaps = max(0, n_chunks - 1)
    if n_gaps == 0:
        return []
    base = remaining / n_gaps
    raw = [base * random.uniform(1 - jitter, 1 + jitter) for _ in range(n_gaps)]
    total = sum(raw) or 1.0
    scale = remaining / total
    return [g * scale for g in raw]


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/admin/stats")
async def admin_stats():
    return dict(_STATE)


@app.get("/v1/models")
@app.get("/models")
async def list_models():
    return {
        "object": "list",
        "data": [{"id": "fake-gpt-4", "object": "model", "owned_by": "mock"}],
    }


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    model = body.get("model", "fake-gpt-4")
    stream = bool(body.get("stream", False))
    ctrl = _parse_mock_control(body)

    seed = ctrl["seed"]
    out_tokens = ctrl["out_tokens"]
    duration_ms = ctrl["duration_ms"]
    ttft_ms = ctrl["ttft_ms"] if ctrl["ttft_ms"] is not None else max(1, int(duration_ms * 0.15))
    ttft_ms = min(ttft_ms, duration_ms)
    jitter = ctrl["jitter"]

    words = text(seed, out_tokens).split(" ") if out_tokens > 0 else []
    response_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    _STATE["inflight"] += 1
    _STATE["total"] += 1
    try:
        if stream:
            async def stream_generator():
                await asyncio.sleep(ttft_ms / 1000.0)
                gaps = _gaps_ms(len(words), ttft_ms, duration_ms, jitter)
                for idx, word in enumerate(words):
                    chunk = {
                        "id": response_id, "object": "chat.completion.chunk",
                        "created": created, "model": model,
                        "choices": [{"index": 0, "delta": {"role": "assistant", "content": word}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
                    if idx < len(gaps):
                        await asyncio.sleep(gaps[idx] / 1000.0)

                done_chunk = {
                    "id": response_id, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": out_tokens, "total_tokens": out_tokens},
                }
                yield f"data: {json.dumps(done_chunk)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(stream_generator(), media_type="text/event-stream")

        await asyncio.sleep(duration_ms / 1000.0)
        content = " ".join(words)
        return {
            "id": response_id, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": out_tokens, "total_tokens": out_tokens},
        }
    finally:
        _STATE["inflight"] -= 1


@app.post("/v1/embeddings")
@app.post("/embeddings")
async def embeddings(request: Request):
    body = await request.json()
    inputs = body.get("input", [""])
    if isinstance(inputs, str):
        inputs = [inputs]
    return {
        "object": "list",
        "data": [{"object": "embedding", "index": i, "embedding": [0.0] * 1536} for i in range(len(inputs))],
        "model": body.get("model", "mock-embedding"),
        "usage": {"prompt_tokens": 5, "total_tokens": 5},
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
