import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from mock_llm.server import app
from bench.detgen import text

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_non_streaming_exact_content_and_usage():
    body = {
        "model": "fake-gpt-4",
        "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
        "mock": {"seed": "abc", "out_tokens": 6, "duration_ms": 50},
    }
    r = client.post("/v1/chat/completions", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["choices"][0]["message"]["content"] == text("abc", 6)
    assert data["usage"]["completion_tokens"] == 6


def test_streaming_exact_content_and_timing():
    body = {
        "model": "fake-gpt-4",
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "mock": {"seed": "xyz", "out_tokens": 5, "duration_ms": 200, "ttft_ms": 40},
    }
    start = time.monotonic()
    with client.stream("POST", "/v1/chat/completions", json=body) as r:
        chunks = []
        for line in r.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            payload = line[len("data: "):]
            if payload == "[DONE]":
                break
            import json
            obj = json.loads(payload)
            delta = obj["choices"][0]["delta"].get("content")
            if delta:
                chunks.append(delta)
    elapsed_ms = (time.monotonic() - start) * 1000
    assert "".join(chunks) == text("xyz", 5)
    assert 150 <= elapsed_ms <= 400  # generous bound, jitter + test overhead


def test_marker_fallback_when_no_mock_key():
    body = {
        "model": "fake-gpt-4",
        "stream": False,
        "messages": [{"role": "user", "content": "[mock seed=fb1 out=3 dur=10 ttft=1] hello"}],
    }
    r = client.post("/v1/chat/completions", json=body)
    data = r.json()
    assert data["choices"][0]["message"]["content"] == text("fb1", 3)


def test_admin_stats():
    r = client.get("/admin/stats")
    assert r.status_code == 200
    assert "inflight" in r.json()


def test_malformed_mock_body_does_not_500():
    body = {
        "model": "fake-gpt-4",
        "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
        "mock": {"seed": "abc", "out_tokens": "not-a-number", "duration_ms": 50},
    }
    r = client.post("/v1/chat/completions", json=body)
    assert r.status_code == 200
    data = r.json()
    # safe fallback: out_tokens=8, deterministic content generated from the
    # fallback seed embedded in the response (content must not be empty).
    content = data["choices"][0]["message"]["content"]
    assert isinstance(content, str) and content != ""
    assert data["usage"]["completion_tokens"] == 8


def test_extreme_jitter_preserves_content_and_bounds_total_time():
    body = {
        "model": "fake-gpt-4",
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "mock": {"seed": "jitter-seed", "out_tokens": 6, "duration_ms": 150, "jitter": 5.0},
    }
    start = time.monotonic()
    with client.stream("POST", "/v1/chat/completions", json=body) as r:
        assert r.status_code == 200
        chunks = []
        for line in r.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            payload = line[len("data: "):]
            if payload == "[DONE]":
                break
            import json
            obj = json.loads(payload)
            delta = obj["choices"][0]["delta"].get("content")
            if delta:
                chunks.append(delta)
    elapsed_ms = (time.monotonic() - start) * 1000
    assert "".join(chunks) == text("jitter-seed", 6)
    # negative gaps clamp to ~0 (asyncio.sleep(negative) is a no-op), so total
    # time should stay in the same ballpark as duration_ms, not blow up.
    assert elapsed_ms <= 600


def test_ttft_contract_time_to_first_chunk(monkeypatch):
    # httpx's ASGITransport (used under TestClient) fully drains the async
    # generator before returning any bytes to the client, so real wall-clock
    # streaming can't be observed this way. Instead, verify the TTFT contract
    # directly: the first `asyncio.sleep` call made while producing the
    # stream must correspond to ttft_ms.
    import mock_llm.server as server_module

    sleep_calls = []
    real_sleep = asyncio.sleep

    async def recording_sleep(seconds):
        sleep_calls.append(seconds)
        await real_sleep(0)  # don't actually wait, keep the test fast

    monkeypatch.setattr(server_module.asyncio, "sleep", recording_sleep)

    body = {
        "model": "fake-gpt-4",
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "mock": {"seed": "ttft-seed", "out_tokens": 4, "duration_ms": 300, "ttft_ms": 150},
    }
    with client.stream("POST", "/v1/chat/completions", json=body) as r:
        assert r.status_code == 200
        for _ in r.iter_lines():
            pass

    assert sleep_calls, "expected at least one asyncio.sleep call"
    first_sleep_ms = sleep_calls[0] * 1000
    assert abs(first_sleep_ms - 150) <= 100  # within +/- 100ms of ttft_ms=150


def test_negative_out_tokens_clamped_to_zero():
    body = {
        "model": "fake-gpt-4",
        "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
        "mock": {"seed": "neg", "out_tokens": -3, "duration_ms": 50},
    }
    r = client.post("/v1/chat/completions", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["choices"][0]["message"]["content"] == ""
    assert data["usage"]["completion_tokens"] == 0
