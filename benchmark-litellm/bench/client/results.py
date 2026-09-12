from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import orjson


@dataclass
class RequestResult:
    i: int
    seed: str
    api: str
    stream: bool
    target: str
    t_sched_ms: float
    t_sent_ms: float
    t_first_byte_ms: float
    t_first_token_ms: float
    t_done_ms: float
    status: int
    ok: bool
    reason: Optional[str]
    chunks: int
    bytes: int
    out_tokens_seen: Optional[int]
    duration_ms: int  # requested duration, from the trace

    @property
    def latency_ms(self) -> float:
        return self.t_done_ms - self.t_sent_ms

    @property
    def ttft_ms(self) -> float:
        return self.t_first_token_ms - self.t_sent_ms

    @property
    def sched_lag_ms(self) -> float:
        return self.t_sent_ms - self.t_sched_ms

    @property
    def overhead_ms(self) -> float:
        return self.latency_ms - self.duration_ms

    def to_dict(self) -> dict:
        d = asdict(self)
        d["latency_ms"] = self.latency_ms
        d["ttft_ms"] = self.ttft_ms
        d["sched_lag_ms"] = self.sched_lag_ms
        d["overhead_ms"] = self.overhead_ms
        return d


class ResultsWriter:
    def __init__(self, path: Path):
        self._f = open(path, "wb")

    def write(self, result: RequestResult) -> None:
        self._f.write(orjson.dumps(result.to_dict()))
        self._f.write(b"\n")

    def close(self) -> None:
        self._f.close()
