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
    assert " ".join(chunks) == text("xyz", 5)
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
