from bench.trace.schema import TraceRequest
from bench.client.openai_chat import build_request as build_chat
from bench.client.anthropic_messages import build_request as build_messages


def _row(**overrides):
    base = dict(
        i=0, t_ms=0, seed="s1", api="chat", stream=True, model="fake-gpt-4",
        in_tokens=10, out_tokens=5, duration_ms=500, ttft_ms=50,
    )
    base.update(overrides)
    return TraceRequest(**base)


def test_build_chat_request_has_mock_key_and_marker():
    url, headers, body = build_chat(_row(), base_url="http://x", api_key="k")
    assert url == "http://x/v1/chat/completions"
    assert headers["Authorization"] == "Bearer k"
    assert body["mock"] == {"seed": "s1", "out_tokens": 5, "duration_ms": 500, "ttft_ms": 50, "jitter": 0.2}
    assert "[mock seed=s1 out=5 dur=500 ttft=50" in body["messages"][0]["content"]
    assert body["stream"] is True


def test_build_chat_request_prompt_padded_to_in_tokens():
    row = _row(in_tokens=20)
    _, _, body = build_chat(row, base_url="http://x", api_key="k")
    # marker + padding words; padding portion should contribute ~in_tokens words
    padding_words = body["messages"][0]["content"].split(" ")
    assert len(padding_words) >= 20


def test_build_messages_request_shape():
    url, headers, body = build_messages(_row(), base_url="http://x", api_key="k")
    assert url == "http://x/v1/messages"
    assert headers["x-api-key"] == "k"
    assert headers["anthropic-version"] == "2023-06-01"
    assert body["max_tokens"] == 5
    assert body["mock"]["seed"] == "s1"
