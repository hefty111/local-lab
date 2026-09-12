import pytest

from bench.trace.synthetic import generate, SyntheticConfig


def test_constant_profile_row_count_and_ordering():
    cfg = SyntheticConfig(
        profile="constant", rps=10.0, duration_s=2.0,
        in_tokens=(50, 50), out_tokens=(20, 20), latency_ms=(1000, 1000),
        stream_ratio=1.0, messages_ratio=0.0, rng_seed=1,
    )
    rows = list(generate(cfg))
    assert len(rows) == 20
    ts = [r.t_ms for r in rows]
    assert ts == sorted(ts)
    assert rows[0].t_ms == 0
    assert all(r.i == idx for idx, r in enumerate(rows))


def test_deterministic_with_same_seed():
    cfg = SyntheticConfig(
        profile="poisson", rps=5.0, duration_s=1.0,
        in_tokens=(10, 100), out_tokens=(5, 50), latency_ms=(100, 2000),
        stream_ratio=0.5, messages_ratio=0.5, rng_seed=42,
    )
    a = list(generate(cfg))
    b = list(generate(cfg))
    assert a == b


def test_seeds_are_unique():
    cfg = SyntheticConfig(
        profile="burst", rps=20.0, duration_s=1.0,
        in_tokens=(1, 1), out_tokens=(1, 1), latency_ms=(1, 1),
        stream_ratio=1.0, messages_ratio=0.0, rng_seed=7,
    )
    rows = list(generate(cfg))
    seeds = {r.seed for r in rows}
    assert len(seeds) == len(rows)


def test_seeds_are_unique_at_large_scale():
    # ~100k rows to actually exercise the seed space and catch collisions.
    cfg = SyntheticConfig(
        profile="constant", rps=2000.0, duration_s=50.0,
        in_tokens=(1, 1), out_tokens=(1, 1), latency_ms=(1, 1),
        stream_ratio=1.0, messages_ratio=0.0, rng_seed=7,
    )
    rows = list(generate(cfg))
    assert len(rows) >= 90_000
    seeds = {r.seed for r in rows}
    assert len(seeds) == len(rows)


def test_ramp_profile_row_count_and_ordering():
    cfg = SyntheticConfig(
        profile="ramp", rps=10.0, duration_s=2.0,
        in_tokens=(50, 50), out_tokens=(20, 20), latency_ms=(1000, 1000),
        stream_ratio=1.0, messages_ratio=0.0, rng_seed=1,
    )
    rows = list(generate(cfg))
    assert len(rows) == 10
    ts = [r.t_ms for r in rows]
    assert ts == sorted(ts)
    assert all(r.i == idx for idx, r in enumerate(rows))


def test_unknown_profile_raises_value_error():
    cfg = SyntheticConfig(
        profile="bogus", rps=10.0, duration_s=1.0,
        in_tokens=(1, 1), out_tokens=(1, 1), latency_ms=(1, 1),
        stream_ratio=1.0, messages_ratio=0.0, rng_seed=1,
    )
    with pytest.raises(ValueError):
        list(generate(cfg))


def test_nonpositive_rps_raises_value_error():
    cfg = SyntheticConfig(
        profile="constant", rps=0.0, duration_s=1.0,
        in_tokens=(1, 1), out_tokens=(1, 1), latency_ms=(1, 1),
        stream_ratio=1.0, messages_ratio=0.0, rng_seed=1,
    )
    with pytest.raises(ValueError):
        list(generate(cfg))


def test_nonpositive_duration_raises_value_error():
    cfg = SyntheticConfig(
        profile="constant", rps=10.0, duration_s=0.0,
        in_tokens=(1, 1), out_tokens=(1, 1), latency_ms=(1, 1),
        stream_ratio=1.0, messages_ratio=0.0, rng_seed=1,
    )
    with pytest.raises(ValueError):
        list(generate(cfg))


def test_messages_ratio_applied():
    cfg = SyntheticConfig(
        profile="constant", rps=100.0, duration_s=1.0,
        in_tokens=(10, 10), out_tokens=(10, 10), latency_ms=(100, 100),
        stream_ratio=1.0, messages_ratio=1.0, rng_seed=3,
    )
    rows = list(generate(cfg))
    assert all(r.api == "messages" for r in rows)
