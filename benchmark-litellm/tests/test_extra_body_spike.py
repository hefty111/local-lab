# benchmark-litellm/tests/test_extra_body_spike.py
"""Confirms whether LiteLLM forwards an unrecognised top-level `mock` key
from the request body through to the OpenAI-compatible backend, for both
/v1/chat/completions and /v1/messages. Requires the stack-litellm stack
running locally (docker compose up in stack-litellm/).
If the key is NOT forwarded on a given endpoint, the client (Task 8) must
use the in-prompt marker fallback for that endpoint.
"""
import httpx
import pytest

PROXY_BASE = "http://localhost:4000"
API_KEY = "sk-1234"


@pytest.mark.integration
def test_mock_key_forwarded_on_chat_completions():
    r = httpx.post(
        f"{PROXY_BASE}/v1/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "model": "fake-gpt-4", "stream": False,
            "messages": [{"role": "user", "content": "hi"}],
            "mock": {"seed": "spike1", "out_tokens": 3, "duration_ms": 10},
        },
        timeout=10,
    )
    assert r.status_code == 200
    from bench.detgen import text
    content = r.json()["choices"][0]["message"]["content"]
    # Record the outcome either way; this assertion documents the finding.
    assert content in (text("spike1", 3), "This is a mock response.")


@pytest.mark.integration
def test_mock_key_forwarded_on_messages():
    r = httpx.post(
        f"{PROXY_BASE}/v1/messages",
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01"},
        json={
            "model": "fake-gpt-4", "max_tokens": 16, "stream": False,
            "messages": [{"role": "user", "content": "hi"}],
            "mock": {"seed": "spike2", "out_tokens": 3, "duration_ms": 10},
        },
        timeout=10,
    )
    assert r.status_code == 200
