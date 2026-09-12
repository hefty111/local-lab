from bench.client.verify import verify_response, VerifyResult
from bench.detgen import text


def test_ok_when_content_and_tokens_match():
    result = verify_response(
        expected_seed="s1", expected_out_tokens=3,
        got_content=text("s1", 3), got_completion_tokens=3,
        stream_terminated=True,
    )
    assert result == VerifyResult(ok=True, reason=None)


def test_fail_on_content_mismatch():
    result = verify_response(
        expected_seed="s1", expected_out_tokens=3,
        got_content="wrong content here", got_completion_tokens=3,
        stream_terminated=True,
    )
    assert result.ok is False
    assert result.reason == "content_mismatch"


def test_fail_on_token_count_mismatch():
    result = verify_response(
        expected_seed="s1", expected_out_tokens=3,
        got_content=text("s1", 3), got_completion_tokens=2,
        stream_terminated=True,
    )
    assert result.ok is False
    assert result.reason == "token_count_mismatch"


def test_fail_on_unterminated_stream():
    result = verify_response(
        expected_seed="s1", expected_out_tokens=3,
        got_content=text("s1", 3), got_completion_tokens=3,
        stream_terminated=False,
    )
    assert result.ok is False
    assert result.reason == "stream_not_terminated"


def test_fail_on_http_error_short_circuits_content_check():
    result = verify_response(
        expected_seed="s1", expected_out_tokens=3,
        got_content=None, got_completion_tokens=None,
        stream_terminated=False, http_status=500,
    )
    assert result.ok is False
    assert result.reason == "http_error"
