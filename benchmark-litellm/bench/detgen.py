"""Deterministic, verifiable content generator shared by the mock server
and the benchmark client. Given the same (seed, n) both sides compute the
identical string without any communication.
"""
import hashlib

# Fixed 2048-word vocabulary, generated once and frozen so results are
# reproducible across processes/machines/versions.
WORDS = [f"tok{n:04x}" for n in range(2048)]


def word_at(seed: str, index: int) -> str:
    digest = hashlib.blake2b(f"{seed}:{index}".encode("utf-8"), digest_size=8).digest()
    idx = int.from_bytes(digest, "big") % len(WORDS)
    return WORDS[idx]


def text(seed: str, n_tokens: int) -> str:
    if n_tokens <= 0:
        return ""
    return " ".join(word_at(seed, i) for i in range(n_tokens))
