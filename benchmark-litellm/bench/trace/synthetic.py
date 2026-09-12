from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Iterator, Tuple

import numpy as np

from bench.trace.schema import TraceRequest


@dataclass(frozen=True)
class SyntheticConfig:
    profile: str  # constant | ramp | burst | poisson
    rps: float
    duration_s: float
    in_tokens: Tuple[int, int]
    out_tokens: Tuple[int, int]
    latency_ms: Tuple[int, int]
    stream_ratio: float
    messages_ratio: float
    rng_seed: int
    model: str = "fake-gpt-4"


def _arrival_times_ms(cfg: SyntheticConfig, rng: np.random.Generator) -> list[float]:
    duration_ms = cfg.duration_s * 1000.0
    if cfg.profile == "constant":
        n = max(1, round(cfg.rps * cfg.duration_s))
        step = duration_ms / n
        return [i * step for i in range(n)]
    if cfg.profile == "ramp":
        # rps ramps linearly from 0 to cfg.rps over the duration
        n = max(1, round(cfg.rps * cfg.duration_s / 2))
        # inverse-CDF sampling of a linear-rate Poisson process is overkill;
        # approximate by evenly spacing over a quadratic time warp.
        fracs = np.linspace(0, 1, n, endpoint=False)
        times = (fracs ** 0.5) * duration_ms
        return sorted(times.tolist())
    if cfg.profile == "burst":
        # 5 bursts of tightly-packed requests
        n_bursts = 5
        per_burst = max(1, round(cfg.rps * cfg.duration_s / n_bursts))
        times = []
        for b in range(n_bursts):
            base = b * duration_ms / n_bursts
            for k in range(per_burst):
                times.append(base + k * 2.0)  # 2ms apart within a burst
        return sorted(times)
    if cfg.profile == "poisson":
        times = []
        t = 0.0
        mean_gap_ms = 1000.0 / cfg.rps
        while t < duration_ms:
            gap = rng.exponential(mean_gap_ms)
            t += gap
            if t < duration_ms:
                times.append(t)
        return times
    raise ValueError(f"unknown profile: {cfg.profile}")


def generate(cfg: SyntheticConfig) -> Iterator[TraceRequest]:
    rng = np.random.default_rng(cfg.rng_seed)
    times = _arrival_times_ms(cfg, rng)
    for i, t_ms in enumerate(times):
        in_tokens = int(rng.integers(cfg.in_tokens[0], cfg.in_tokens[1] + 1))
        out_tokens = int(rng.integers(cfg.out_tokens[0], cfg.out_tokens[1] + 1))
        duration_ms = int(rng.integers(cfg.latency_ms[0], cfg.latency_ms[1] + 1))
        stream = bool(rng.random() < cfg.stream_ratio)
        api = "messages" if rng.random() < cfg.messages_ratio else "chat"
        ttft_ms = max(1, int(duration_ms * 0.15)) if stream else None
        rand_bits = int(rng.integers(0, 2**63 - 1)) ^ (int(rng.integers(0, 2**63 - 1)) << 32)
        seed = uuid.UUID(int=(rand_bits ^ (cfg.rng_seed << 32) ^ i) & ((1 << 128) - 1)).hex[:16]
        yield TraceRequest(
            i=i, t_ms=int(t_ms), seed=seed, api=api, stream=stream,
            model=cfg.model, in_tokens=in_tokens, out_tokens=out_tokens,
            duration_ms=duration_ms, ttft_ms=ttft_ms,
        )
