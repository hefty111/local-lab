from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Iterable, Iterator


def iter_fire_times(offsets_ms: Iterable[int], time_scale: float) -> Iterator[float]:
    for offset in offsets_ms:
        yield offset * time_scale


async def run_schedule(
    offsets_ms: list[int],
    time_scale: float,
    callback: Callable[[int], Awaitable[None]],
    max_inflight: int = 500,
) -> None:
    """Fires `callback(i)` for each row index i at its scaled offset,
    relative to the moment run_schedule was called. Never awaits the
    callback itself (fire-and-forget via create_task) so a slow request
    doesn't delay later arrivals, bounded by a semaphore so the client
    doesn't unbounded-spawn tasks."""
    sem = asyncio.Semaphore(max_inflight)
    start = time.monotonic()
    tasks = []

    async def _guarded(i: int):
        async with sem:
            await callback(i)

    for i, fire_ms in enumerate(iter_fire_times(offsets_ms, time_scale)):
        target = start + fire_ms / 1000.0
        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        tasks.append(asyncio.create_task(_guarded(i)))

    if tasks:
        await asyncio.gather(*tasks)
