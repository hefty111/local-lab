from __future__ import annotations

from bench.detgen import text
from bench.trace.schema import TraceRequest


def _marker(row: TraceRequest, jitter: float = 0.2) -> str:
    ttft_part = f" ttft={row.ttft_ms}" if row.ttft_ms is not None else ""
    return f"[mock seed={row.seed} out={row.out_tokens} dur={row.duration_ms}{ttft_part} jitter={jitter}]"


def build_request(row: TraceRequest, base_url: str, api_key: str, jitter: float = 0.2):
    prompt_padding = text(row.seed + ":prompt", row.in_tokens)
    content = f"{_marker(row, jitter)} {prompt_padding}"
    body = {
        "model": row.model,
        "stream": row.stream,
        "messages": [{"role": "user", "content": content}],
        "mock": {

            "seed": row.seed,
            "out_tokens": row.out_tokens,
            "duration_ms": row.duration_ms,
            "ttft_ms": row.ttft_ms,
            "jitter": jitter,
        },
    }
    if row.stream:
        # Ask OpenAI-compatible backends (real LiteLLM proxy included) to emit
        # a final usage-bearing chunk on streamed responses. Without this,
        # many proxies/backends omit `usage` entirely on SSE streams. Harmless
        # if a backend doesn't understand the flag (ignored).
        body["stream_options"] = {"include_usage": True}
    headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    return url, headers, body
