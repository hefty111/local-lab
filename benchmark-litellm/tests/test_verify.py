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


def test_ok_when_content_matches_but_token_count_differs_plausibly():
    # A real proxy re-tokenizes with its own tokenizer (e.g. tiktoken) and
    # will almost never agree with the mock's word-count-based total, even
    # though the content is correct. This must not be a hard failure.
    result = verify_response(
        expected_seed="s1", expected_out_tokens=15,
        got_content=text("s1", 15), got_completion_tokens=46,
        stream_terminated=True,
    )
    assert result == VerifyResult(ok=True, reason=None)


def test_fail_on_missing_token_count():
    result = verify_response(
        expected_seed="s1", expected_out_tokens=3,
        got_content=text("s1", 3), got_completion_tokens=None,
        stream_terminated=True,
    )
    assert result.ok is False
    assert result.reason == "token_count_missing"


def test_fail_on_zero_token_count_when_output_expected():
    result = verify_response(
        expected_seed="s1", expected_out_tokens=3,
        got_content=text("s1", 3), got_completion_tokens=0,
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
