from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from bench.trace.schema import TraceRequest, write_jsonl

QUERY = """
SELECT "request_id", "startTime", "endTime", "completionStartTime",
       "prompt_tokens", "completion_tokens", "call_type", "model"
FROM "LiteLLM_SpendLogs"
WHERE "startTime" BETWEEN %(start)s AND %(end)s
ORDER BY "startTime"
"""

ANTHROPIC_CALL_TYPES = {"anthropic_messages", "aanthropic_messages"}


def should_sample(request_id: str, fraction: float) -> bool:
    if fraction >= 1.0:
        return True
    if fraction <= 0.0:
        return False
    digest = hashlib.blake2b(request_id.encode("utf-8"), digest_size=8).digest()
    bucket = int.from_bytes(digest, "big") % 10_000
    return bucket < int(fraction * 10_000)


def row_to_trace(
    row: dict,
    first_start: datetime,
    i: int,
    model_map: dict[str, str],
) -> Optional[TraceRequest]:
    in_tokens = row["prompt_tokens"] or 0
    out_tokens = row["completion_tokens"] or 0
    if in_tokens <= 0 or out_tokens <= 0:
        return None

    start = row["startTime"]
    end = row["endTime"]
    if end is None or start is None:
        return None
    duration_ms = int((end - start).total_seconds() * 1000)
    if duration_ms <= 0:
        return None

    completion_start = row.get("completionStartTime")
    ttft_ms = None
    stream = False
    if completion_start is not None:
        ttft_ms = max(1, int((completion_start - start).total_seconds() * 1000))
        stream = True

    call_type = row.get("call_type") or "acompletion"
    api = "messages" if call_type in ANTHROPIC_CALL_TYPES else "chat"

    model = model_map.get(row["model"], model_map.get("*", "fake-gpt-4"))
    t_ms = int((start - first_start).total_seconds() * 1000)

    return TraceRequest(
        i=i, t_ms=t_ms, seed=row["request_id"], api=api, stream=stream,
        model=model, in_tokens=in_tokens, out_tokens=out_tokens,
        duration_ms=duration_ms, ttft_ms=ttft_ms,
    )


def iter_from_db(
    dsn: str,
    start: datetime,
    end: datetime,
    sample: float = 1.0,
    limit: Optional[int] = None,
    model_map: Optional[dict[str, str]] = None,
) -> Iterator[TraceRequest]:
    import psycopg

    model_map = model_map or {}
    dropped = 0
    kept_i = 0
    first_start: Optional[datetime] = None

    with psycopg.connect(dsn) as conn:
        with conn.cursor(name="bench_spendlogs_cursor") as cur:
            cur.itersize = 5000
            cur.execute(QUERY, {"start": start, "end": end})
            for record in cur:
                row = dict(zip(
                    ["request_id", "startTime", "endTime", "completionStartTime",
                     "prompt_tokens", "completion_tokens", "call_type", "model"],
                    record,
                ))
                if not should_sample(row["request_id"], sample):
                    continue
                if first_start is None:
                    first_start = row["startTime"]
                tr = row_to_trace(row, first_start=first_start, i=kept_i, model_map=model_map)
                if tr is None:
                    dropped += 1
                    continue
                yield tr
                kept_i += 1
                if limit is not None and kept_i >= limit:
                    break

    print(f"spendlogs: kept={kept_i} dropped={dropped}", file=sys.stderr)


def iter_from_csv(path: Path, sample: float = 1.0, model_map: Optional[dict[str, str]] = None) -> Iterator[TraceRequest]:
    import csv
    from datetime import datetime as dt

    model_map = model_map or {}
    dropped = 0
    kept_i = 0
    first_start: Optional[datetime] = None

    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            row = {
                "request_id": raw["request_id"],
                "startTime": dt.fromisoformat(raw["startTime"]),
                "endTime": dt.fromisoformat(raw["endTime"]) if raw.get("endTime") else None,
                "completionStartTime": dt.fromisoformat(raw["completionStartTime"]) if raw.get("completionStartTime") else None,
                "prompt_tokens": int(raw["prompt_tokens"] or 0),
                "completion_tokens": int(raw["completion_tokens"] or 0),
                "call_type": raw.get("call_type"),
                "model": raw.get("model"),
            }
            if not should_sample(row["request_id"], sample):
                continue
            if first_start is None:
                first_start = row["startTime"]
            tr = row_to_trace(row, first_start=first_start, i=kept_i, model_map=model_map)
            if tr is None:
                dropped += 1
                continue
            yield tr
            kept_i += 1

    print(f"spendlogs(csv): kept={kept_i} dropped={dropped}", file=sys.stderr)
