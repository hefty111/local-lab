from __future__ import annotations

from bench.detgen import text
from bench.trace.schema import TraceRequest
from bench.client.openai_chat import _marker


def build_request(row: TraceRequest, base_url: str, api_key: str, jitter: float = 0.2):
    prompt_padding = text(row.seed + ":prompt", row.in_tokens)
    content = f"{_marker(row, jitter)} {prompt_padding}"
    body = {
        "model": row.model,
        "stream": row.stream,
        "max_tokens": row.out_tokens,
        "messages": [{"role": "user", "content": content}],
        "mock": {
            "seed": row.seed,
            "out_tokens": row.out_tokens,
            "duration_ms": row.duration_ms,
            "ttft_ms": row.ttft_ms,
            "jitter": jitter,
        },
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    url = f"{base_url.rstrip('/')}/v1/messages"
    return url, headers, body
