from bench.client.sse import parse_openai_chat_stream, parse_anthropic_messages_stream


def _sse_lines(*payloads):
    for p in payloads:
        yield f"data: {p}"
        yield ""


def test_parse_openai_chat_stream_joins_content_and_reports_tokens():
    lines = list(_sse_lines(
        '{"choices":[{"delta":{"role":"assistant","content":"foo"}}]}',
        '{"choices":[{"delta":{"content":" bar"}}]}',
        '{"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"completion_tokens":2}}',
        "[DONE]",
    ))
    result = parse_openai_chat_stream(lines)
    assert result.content == "foo bar"
    assert result.completion_tokens == 2
    assert result.terminated is True


def test_parse_openai_chat_stream_not_terminated_if_no_done():
    lines = list(_sse_lines(
        '{"choices":[{"delta":{"content":"foo"}}]}',
    ))
    result = parse_openai_chat_stream(lines)
    assert result.terminated is False


def test_parse_anthropic_messages_stream():
    lines = list(_sse_lines(
        '{"type":"message_start","message":{"usage":{"output_tokens":0}}}',
        '{"type":"content_block_delta","delta":{"type":"text_delta","text":"foo"}}',
        '{"type":"content_block_delta","delta":{"type":"text_delta","text":" bar"}}',
        '{"type":"message_delta","usage":{"output_tokens":2}}',
        '{"type":"message_stop"}',
    ))
    result = parse_anthropic_messages_stream(lines)
    assert result.content == "foo bar"
    assert result.completion_tokens == 2
    assert result.terminated is True


def test_parse_openai_chat_stream_skips_malformed_json_line():
    lines = list(_sse_lines(
        '{"choices":[{"delta":{"content":"foo"}}]}',
        '{not valid json!!',
        '{"choices":[{"delta":{"content":" bar"}}]}',
        "[DONE]",
    ))
    result = parse_openai_chat_stream(lines)
    assert result.content == "foo bar"
    assert result.terminated is True


def test_parse_openai_chat_stream_skips_non_dict_payload():
    lines = list(_sse_lines(
        "true",
        '{"choices":[{"delta":{"content":"foo"}}]}',
    ))
    result = parse_openai_chat_stream(lines)
    assert result.content == "foo"


def test_parse_anthropic_messages_stream_skips_malformed_json_line():
    lines = list(_sse_lines(
        '{"type":"content_block_delta","delta":{"type":"text_delta","text":"foo"}}',
        '{broken',
        '{"type":"content_block_delta","delta":{"type":"text_delta","text":" bar"}}',
        '{"type":"message_stop"}',
    ))
    result = parse_anthropic_messages_stream(lines)
    assert result.content == "foo bar"
    assert result.terminated is True


def test_parse_anthropic_messages_stream_skips_non_dict_payload():
    lines = list(_sse_lines(
        "true",
        '{"type":"content_block_delta","delta":{"type":"text_delta","text":"foo"}}',
    ))
    result = parse_anthropic_messages_stream(lines)
    assert result.content == "foo"
