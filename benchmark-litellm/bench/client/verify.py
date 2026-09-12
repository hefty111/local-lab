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

    if got_completion_tokens != expected_out_tokens:
        return VerifyResult(ok=False, reason="token_count_mismatch")

    return VerifyResult(ok=True, reason=None)
