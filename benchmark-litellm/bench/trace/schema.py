from __future__ import annotations

from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterator, Optional

import orjson


@dataclass(frozen=True)
class TraceRequest:
    i: int
    t_ms: int
    seed: str
    api: str            # "chat" | "messages"
    stream: bool
    model: str
    in_tokens: int
    out_tokens: int
    duration_ms: int
    ttft_ms: Optional[int] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["ttft_ms"] is None:
            del d["ttft_ms"]
        return d

    @staticmethod
    def from_dict(d: dict) -> "TraceRequest":
        return TraceRequest(**d)


def write_jsonl(path: Path, rows: Iterator[TraceRequest]) -> int:
    count = 0
    with open(path, "wb") as f:
        for row in rows:
            f.write(orjson.dumps(row.to_dict()))
            f.write(b"\n")
            count += 1
    return count


def read_jsonl(path: Path) -> Iterator[TraceRequest]:
    with open(path, "rb") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield TraceRequest.from_dict(orjson.loads(line))
