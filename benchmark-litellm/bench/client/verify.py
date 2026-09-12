from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from bench.detgen import text


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    reason: Optional[str]


def verify_response(
    expected_seed: str,
    expected_out_tokens: int,
    got_content: Optional[str],
    got_completion_tokens: Optional[int],
    stream_terminated: bool,
    http_status: int = 200,
) -> VerifyResult:
    if http_status != 200:
        return VerifyResult(ok=False, reason="http_error")

    if not stream_terminated:
        return VerifyResult(ok=False, reason="stream_not_terminated")

    expected_content = text(expected_seed, expected_out_tokens)
    if got_content != expected_content:
        return VerifyResult(ok=False, reason="content_mismatch")

    # Token-count check is intentionally *not* an exact-equality check.
    #
    # Rationale: the mock server's "tokens" are simply space-separated
    # dictionary words from a deterministic generator (see bench/detgen.py).
    # When talking to the mock directly, it honestly reports
    # completion_tokens == out_tokens, so exact equality holds.
    #
    # But when requests go through a real LiteLLM proxy, LiteLLM recomputes
    # `usage.completion_tokens` by re-tokenizing the actual response content
    # with a real subword tokenizer (e.g. tiktoken), rather than passing
    # through whatever the upstream reported. Because the mock's "tokens"
    # are not real subword tokens, the proxy's recomputed count will almost
    # never equal the mock's synthetic target, even though the content is
    # byte-for-byte correct. Treating that mismatch as a hard failure
    # produced 100% false-positive failures on real-proxy runs.
    #
    # Content equality (checked above) is the actual ground truth for
    # correctness here: if the bytes match what bench/detgen.text() would
    # deterministically produce for (seed, out_tokens), the response is
    # correct regardless of which tokenizer counted it. So token count is
    # downgraded to an instrumentation sanity check rather than a
    # content-correctness check: we only fail if the reported count is
    # missing or structurally nonsensical, which are the two failure modes
    # that actually indicate broken telemetry/instrumentation rather than
    # tokenizer disagreement:
    #   - got_completion_tokens is None: the backend/proxy didn't report
    #     usage at all, even though we explicitly requested it (see
    #     stream_options.include_usage in openai_chat.py). That's a real
    #     instrumentation gap worth failing on.
    #   - got_completion_tokens <= 0 while we expected non-zero output:
    #     a clear sign the reported usage is broken/zeroed-out, not just a
    #     different tokenizer's opinion of the count.
    if expected_out_tokens > 0:
        if got_completion_tokens is None:
            return VerifyResult(ok=False, reason="token_count_missing")
        if got_completion_tokens <= 0:
            return VerifyResult(ok=False, reason="token_count_mismatch")

    return VerifyResult(ok=True, reason=None)
