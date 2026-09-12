import asyncio

import pytest

from bench.client.scheduler import iter_fire_times


def test_iter_fire_times_scales_and_orders():
    offsets_ms = [0, 100, 300]
    times = list(iter_fire_times(offsets_ms, time_scale=0.5))
    assert times == [0.0, 50.0, 150.0]


@pytest.mark.asyncio
async def test_run_schedule_invokes_callback_in_order_and_respects_delay():
    from bench.client.scheduler import run_schedule

    fired = []

    async def cb(i):
        fired.append(i)

    offsets_ms = [0, 20, 40]
    await run_schedule(offsets_ms, time_scale=1.0, callback=cb, max_inflight=10)
    assert fired == [0, 1, 2]


@pytest.mark.asyncio
async def test_run_schedule_survives_callback_exception():
    from bench.client.scheduler import run_schedule

    fired = []

    async def cb(i):
        if i == 1:
            raise RuntimeError("boom")
        fired.append(i)

    offsets_ms = [0, 10, 20]
    # Should not raise even though callback(1) raises.
    await run_schedule(offsets_ms, time_scale=1.0, callback=cb, max_inflight=10)
    assert fired == [0, 2]
