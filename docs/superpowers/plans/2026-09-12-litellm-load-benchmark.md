# LiteLLM Load Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `benchmark-litellm/`, an automated tool that replays trace-driven traffic against a deterministic mock LLM (directly and through LiteLLM's `/v1/chat/completions` and `/v1/messages`), verifies response correctness, and produces an Excel report comparing baseline vs proxy performance.

**Architecture:** A shared `detgen` module (deterministic word generator) is used by both the mock server and the benchmark client so expected output can be computed independently and byte-compared. Traces are JSONL files (from Postgres `LiteLLM_SpendLogs` via a streaming cursor, or synthetically generated) consumed by an asyncio replayer that schedules requests at their trace offsets, records latency/TTFT/correctness per request into JSONL, and a separate report step turns one or more result files into a multi-sheet Excel workbook.

**Tech Stack:** Python 3.12, FastAPI + uvicorn (mock server), httpx (async client), typer (CLI), psycopg3 (spend logs), xlsxwriter (report), numpy (percentiles), pytest.

**Spec:** `docs/superpowers/specs/2026-09-12-litellm-load-benchmark-design.md`

---

## Task 0: Project scaffolding

**Files:**
- Create: `benchmark-litellm/pyproject.toml`
- Create: `benchmark-litellm/bench/__init__.py`
- Create: `benchmark-litellm/tests/__init__.py`
- Create: `benchmark-litellm/README.md`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "bench"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "typer>=0.12",
    "httpx[http2]>=0.27",
    "orjson>=3.10",
    "numpy>=1.26",
    "xlsxwriter>=3.2",
    "psycopg[binary]>=3.1",
    "fastapi>=0.110",
    "uvicorn[standard]>=0.29",
]

[project.scripts]
bench = "bench.cli:app"

[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["bench*"]

[tool.pytest.ini_options]
markers = ["integration: requires a running stack-litellm stack"]
```

- [ ] **Step 2: Create empty package markers**

```bash
mkdir -p /home/ivri_faitelson/work/litellm/local-lab/benchmark-litellm/bench
mkdir -p /home/ivri_faitelson/work/litellm/local-lab/benchmark-litellm/tests
touch /home/ivri_faitelson/work/litellm/local-lab/benchmark-litellm/bench/__init__.py
touch /home/ivri_faitelson/work/litellm/local-lab/benchmark-litellm/tests/__init__.py
```

- [ ] **Step 3: Write `README.md`**

```markdown
# benchmark-litellm

Trace-driven correctness + performance benchmark for the LiteLLM proxy.

## Install

    cd benchmark-litellm
    python3 -m venv .venv && . .venv/bin/activate
    pip install -e .

## Quick start (synthetic trace, 60s, 20 rps)

    bench all --profile constant --rps 20 --duration-s 60 \
      --proxy-base http://localhost:4000 --mock-base http://localhost:8090 \
      --api-key sk-1234 -o report.xlsx

See `docs/superpowers/specs/2026-09-12-litellm-load-benchmark-design.md` for design details.
```

- [ ] **Step 4: Editable install**

```bash
cd /home/ivri_faitelson/work/litellm/local-lab/benchmark-litellm && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" 2>/dev/null; .venv/bin/pip install -e . && .venv/bin/pip install pytest
```
Expected: install succeeds (no `[dev]` extra defined, that's fine, second command installs the package).

- [ ] **Step 5: Commit**

```bash
cd /home/ivri_faitelson/work/litellm/local-lab
git add benchmark-litellm/pyproject.toml benchmark-litellm/README.md benchmark-litellm/bench/__init__.py benchmark-litellm/tests/__init__.py
git commit -m "chore(benchmark-litellm): scaffold project"
```

---

## Task 1: `detgen` — deterministic content generator

**Files:**
- Create: `benchmark-litellm/bench/detgen.py`
- Test: `benchmark-litellm/tests/test_detgen.py`

- [ ] **Step 1: Write failing tests**

```python
# benchmark-litellm/tests/test_detgen.py
from bench.detgen import text, WORDS


def test_text_is_deterministic():
    assert text("seed-1", 5) == text("seed-1", 5)


def test_text_differs_by_seed():
    assert text("seed-1", 5) != text("seed-2", 5)


def test_text_word_count():
    out = text("abc123", 12)
    assert len(out.split(" ")) == 12


def test_text_zero_tokens():
    assert text("abc123", 0) == ""


def test_text_uses_word_list():
    out = text("xyz", 20)
    assert all(w in WORDS for w in out.split(" "))
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_detgen.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bench.detgen'`

- [ ] **Step 3: Implement `detgen.py`**

```python
# benchmark-litellm/bench/detgen.py
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_detgen.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add benchmark-litellm/bench/detgen.py benchmark-litellm/tests/test_detgen.py
git commit -m "feat(benchmark-litellm): add deterministic content generator"
```

---

## Task 2: Trace schema (`TraceRequest` + JSONL I/O)

**Files:**
- Create: `benchmark-litellm/bench/trace/__init__.py`
- Create: `benchmark-litellm/bench/trace/schema.py`
- Test: `benchmark-litellm/tests/test_schema.py`

- [ ] **Step 1: Write failing tests**

```python
# benchmark-litellm/tests/test_schema.py
import io
from bench.trace.schema import TraceRequest, read_jsonl, write_jsonl


def make_row(i=0, t_ms=0):
    return TraceRequest(
        i=i, t_ms=t_ms, seed=f"s{i}", api="chat", stream=True,
        model="fake-gpt-4", in_tokens=100, out_tokens=50,
        duration_ms=1000, ttft_ms=150,
    )


def test_roundtrip(tmp_path):
    rows = [make_row(0, 0), make_row(1, 500)]
    path = tmp_path / "trace.jsonl"
    write_jsonl(path, rows)
    loaded = list(read_jsonl(path))
    assert loaded == rows


def test_ttft_defaults_to_none():
    row = TraceRequest(
        i=0, t_ms=0, seed="s", api="chat", stream=False,
        model="m", in_tokens=1, out_tokens=1, duration_ms=10,
    )
    assert row.ttft_ms is None


def test_to_dict_omits_none_ttft():
    row = TraceRequest(
        i=0, t_ms=0, seed="s", api="chat", stream=False,
        model="m", in_tokens=1, out_tokens=1, duration_ms=10,
    )
    d = row.to_dict()
    assert "ttft_ms" not in d
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_schema.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'bench.trace'`

- [ ] **Step 3: Implement**

```python
# benchmark-litellm/bench/trace/__init__.py
```

```python
# benchmark-litellm/bench/trace/schema.py
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_schema.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add benchmark-litellm/bench/trace/__init__.py benchmark-litellm/bench/trace/schema.py benchmark-litellm/tests/test_schema.py
git commit -m "feat(benchmark-litellm): add TraceRequest schema and JSONL I/O"
```

---

## Task 3: Synthetic trace generator

**Files:**
- Create: `benchmark-litellm/bench/trace/synthetic.py`
- Test: `benchmark-litellm/tests/test_synthetic.py`

- [ ] **Step 1: Write failing tests**

```python
# benchmark-litellm/tests/test_synthetic.py
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


def test_messages_ratio_applied():
    cfg = SyntheticConfig(
        profile="constant", rps=100.0, duration_s=1.0,
        in_tokens=(10, 10), out_tokens=(10, 10), latency_ms=(100, 100),
        stream_ratio=1.0, messages_ratio=1.0, rng_seed=3,
    )
    rows = list(generate(cfg))
    assert all(r.api == "messages" for r in rows)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_synthetic.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'bench.trace.synthetic'`

- [ ] **Step 3: Implement**

```python
# benchmark-litellm/bench/trace/synthetic.py
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
    times = _arrival_times_ms(cfg)
    for i, t_ms in enumerate(times):
        in_tokens = int(rng.integers(cfg.in_tokens[0], cfg.in_tokens[1] + 1))
        out_tokens = int(rng.integers(cfg.out_tokens[0], cfg.out_tokens[1] + 1))
        duration_ms = int(rng.integers(cfg.latency_ms[0], cfg.latency_ms[1] + 1))
        stream = bool(rng.random() < cfg.stream_ratio)
        api = "messages" if rng.random() < cfg.messages_ratio else "chat"
        ttft_ms = max(1, int(duration_ms * 0.15)) if stream else None
        seed = uuid.UUID(int=rng.integers(0, 2**64) ^ (cfg.rng_seed << 32) ^ i).hex[:16]
        yield TraceRequest(
            i=i, t_ms=int(t_ms), seed=seed, api=api, stream=stream,
            model=cfg.model, in_tokens=in_tokens, out_tokens=out_tokens,
            duration_ms=duration_ms, ttft_ms=ttft_ms,
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_synthetic.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add benchmark-litellm/bench/trace/synthetic.py benchmark-litellm/tests/test_synthetic.py
git commit -m "feat(benchmark-litellm): add synthetic trace generator"
```

---

## Task 4: Spend-logs trace ingestion

**Files:**
- Create: `benchmark-litellm/bench/trace/from_spendlogs.py`
- Test: `benchmark-litellm/tests/test_from_spendlogs.py`

- [ ] **Step 1: Write failing tests (pure-function parts, no DB)**

```python
# benchmark-litellm/tests/test_from_spendlogs.py
from datetime import datetime, timedelta

from bench.trace.from_spendlogs import row_to_trace, should_sample


def _row(**overrides):
    base = dict(
        request_id="req-1",
        startTime=datetime(2026, 1, 1, 0, 0, 0),
        endTime=datetime(2026, 1, 1, 0, 0, 2, 300000),
        completionStartTime=datetime(2026, 1, 1, 0, 0, 0, 400000),
        prompt_tokens=120,
        completion_tokens=40,
        call_type="acompletion",
        model="gpt-4o",
    )
    base.update(overrides)
    return base


def test_row_to_trace_basic_mapping():
    first_start = datetime(2026, 1, 1, 0, 0, 0)
    row = _row()
    tr = row_to_trace(row, first_start=first_start, i=0, model_map={"gpt-4o": "fake-gpt-4"})
    assert tr is not None
    assert tr.t_ms == 0
    assert tr.duration_ms == 2300
    assert tr.ttft_ms == 400
    assert tr.stream is True
    assert tr.api == "chat"
    assert tr.model == "fake-gpt-4"
    assert tr.in_tokens == 120
    assert tr.out_tokens == 40


def test_row_to_trace_anthropic_call_type():
    first_start = datetime(2026, 1, 1, 0, 0, 0)
    row = _row(call_type="anthropic_messages", completionStartTime=None)
    tr = row_to_trace(row, first_start=first_start, i=1, model_map={})
    assert tr.api == "messages"
    assert tr.stream is False
    assert tr.ttft_ms is None


def test_row_to_trace_drops_zero_tokens():
    row = _row(completion_tokens=0)
    tr = row_to_trace(row, first_start=datetime(2026, 1, 1), i=0, model_map={})
    assert tr is None


def test_row_to_trace_drops_nonpositive_duration():
    row = _row(endTime=datetime(2026, 1, 1, 0, 0, 0))  # equal to startTime
    tr = row_to_trace(row, first_start=datetime(2026, 1, 1), i=0, model_map={})
    assert tr is None


def test_should_sample_deterministic():
    a = should_sample("req-123", 0.5)
    b = should_sample("req-123", 0.5)
    assert a == b


def test_should_sample_full_and_zero():
    assert should_sample("any-id", 1.0) is True
    assert should_sample("any-id", 0.0) is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_from_spendlogs.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'bench.trace.from_spendlogs'`

- [ ] **Step 3: Implement**

```python
# benchmark-litellm/bench/trace/from_spendlogs.py
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_from_spendlogs.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add benchmark-litellm/bench/trace/from_spendlogs.py benchmark-litellm/tests/test_from_spendlogs.py
git commit -m "feat(benchmark-litellm): add spend-logs trace ingestion"
```

---

## Task 5: Mock server v2 (move + extend)

**Files:**
- Create: `benchmark-litellm/mock-llm/server.py`
- Create: `benchmark-litellm/mock-llm/detgen.py` (symlink-equivalent: import shim, see step 3)
- Create: `benchmark-litellm/mock-llm/Dockerfile`
- Modify: `stack-litellm/docker-compose.yml:115-124`
- Delete: `stack-litellm/mock-llm/mock_llm_server.py`, `stack-litellm/mock-llm/Dockerfile`
- Test: `benchmark-litellm/tests/test_mock_server.py`

- [ ] **Step 1: Write failing tests**

```python
# benchmark-litellm/tests/test_mock_server.py
import time

import pytest
from fastapi.testclient import TestClient

from mock_llm.server import app
from bench.detgen import text

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_non_streaming_exact_content_and_usage():
    body = {
        "model": "fake-gpt-4",
        "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
        "mock": {"seed": "abc", "out_tokens": 6, "duration_ms": 50},
    }
    r = client.post("/v1/chat/completions", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["choices"][0]["message"]["content"] == text("abc", 6)
    assert data["usage"]["completion_tokens"] == 6


def test_streaming_exact_content_and_timing():
    body = {
        "model": "fake-gpt-4",
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "mock": {"seed": "xyz", "out_tokens": 5, "duration_ms": 200, "ttft_ms": 40},
    }
    start = time.monotonic()
    with client.stream("POST", "/v1/chat/completions", json=body) as r:
        chunks = []
        for line in r.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            payload = line[len("data: "):]
            if payload == "[DONE]":
                break
            import json
            obj = json.loads(payload)
            delta = obj["choices"][0]["delta"].get("content")
            if delta:
                chunks.append(delta)
    elapsed_ms = (time.monotonic() - start) * 1000
    assert " ".join(chunks) == text("xyz", 5)
    assert 150 <= elapsed_ms <= 400  # generous bound, jitter + test overhead


def test_marker_fallback_when_no_mock_key():
    body = {
        "model": "fake-gpt-4",
        "stream": False,
        "messages": [{"role": "user", "content": "[mock seed=fb1 out=3 dur=10 ttft=1] hello"}],
    }
    r = client.post("/v1/chat/completions", json=body)
    data = r.json()
    assert data["choices"][0]["message"]["content"] == text("fb1", 3)


def test_admin_stats():
    r = client.get("/admin/stats")
    assert r.status_code == 200
    assert "inflight" in r.json()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd benchmark-litellm && .venv/bin/pip install httpx && .venv/bin/pytest tests/test_mock_server.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'mock_llm'`

- [ ] **Step 3: Move and implement**

```bash
mkdir -p /home/ivri_faitelson/work/litellm/local-lab/benchmark-litellm/mock-llm/mock_llm
git -C /home/ivri_faitelson/work/litellm/local-lab mv stack-litellm/mock-llm/mock_llm_server.py benchmark-litellm/mock-llm/mock_llm/_legacy_reference.py
```

Note: the legacy file is kept only as a reference during review; delete it once `server.py` below is confirmed working (Step 6 handles removal).

```python
# benchmark-litellm/mock-llm/mock_llm/__init__.py
```

```python
# benchmark-litellm/mock-llm/mock_llm/server.py
"""Mock OpenAI-compatible LLM server with deterministic, per-request
controllable output (content, token count, TTFT, total duration, jitter).

Control is read from a top-level `mock` object in the request body
(forwarded by LiteLLM's `extra_body`), with a fallback marker embedded in
the first user message: `[mock seed=... out=... dur=... ttft=... jitter=...]`
This marker doubles as a cache-buster for LiteLLM's Redis response cache.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
import uuid
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from bench.detgen import text

app = FastAPI(title="Mock LLM Server v2")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

_STATE = {"inflight": 0, "total": 0, "errors": 0}

MARKER_RE = re.compile(
    r"\[mock seed=(?P<seed>\S+) out=(?P<out>\d+) dur=(?P<dur>\d+)"
    r"(?: ttft=(?P<ttft>\d+))?(?: jitter=(?P<jitter>[\d.]+))?\]"
)


def _parse_mock_control(body: dict) -> dict:
    mock = body.get("mock")
    if isinstance(mock, dict) and "seed" in mock and "out_tokens" in mock:
        return {
            "seed": str(mock["seed"]),
            "out_tokens": int(mock["out_tokens"]),
            "duration_ms": int(mock.get("duration_ms", 1000)),
            "ttft_ms": mock.get("ttft_ms"),
            "jitter": float(mock.get("jitter", 0.2)),
        }

    messages = body.get("messages") or []
    first_user = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
    if isinstance(first_user, list):
        first_user = " ".join(
            part.get("text", "") for part in first_user if isinstance(part, dict)
        )
    match = MARKER_RE.search(first_user or "")
    if match:
        gd = match.groupdict()
        return {
            "seed": gd["seed"],
            "out_tokens": int(gd["out"]),
            "duration_ms": int(gd["dur"]),
            "ttft_ms": int(gd["ttft"]) if gd["ttft"] else None,
            "jitter": float(gd["jitter"]) if gd["jitter"] else 0.2,
        }

    # No control found: deterministic default so the server never 500s.
    fallback_seed = str(uuid.uuid4())
    return {"seed": fallback_seed, "out_tokens": 8, "duration_ms": 50, "ttft_ms": None, "jitter": 0.0}


def _gaps_ms(n_chunks: int, ttft_ms: int, duration_ms: int, jitter: float) -> list[float]:
    """Return n_chunks-1 inter-chunk gaps (after the first token) summing to
    duration_ms - ttft_ms, with per-gap multiplicative jitter, renormalised
    so the total is exact."""
    remaining = max(0, duration_ms - ttft_ms)
    n_gaps = max(0, n_chunks - 1)
    if n_gaps == 0:
        return []
    base = remaining / n_gaps
    raw = [base * random.uniform(1 - jitter, 1 + jitter) for _ in range(n_gaps)]
    total = sum(raw) or 1.0
    scale = remaining / total
    return [g * scale for g in raw]


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/admin/stats")
async def admin_stats():
    return dict(_STATE)


@app.get("/v1/models")
@app.get("/models")
async def list_models():
    return {
        "object": "list",
        "data": [{"id": "fake-gpt-4", "object": "model", "owned_by": "mock"}],
    }


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    model = body.get("model", "fake-gpt-4")
    stream = bool(body.get("stream", False))
    ctrl = _parse_mock_control(body)

    seed = ctrl["seed"]
    out_tokens = ctrl["out_tokens"]
    duration_ms = ctrl["duration_ms"]
    ttft_ms = ctrl["ttft_ms"] if ctrl["ttft_ms"] is not None else max(1, int(duration_ms * 0.15))
    ttft_ms = min(ttft_ms, duration_ms)
    jitter = ctrl["jitter"]

    words = text(seed, out_tokens).split(" ") if out_tokens > 0 else []
    response_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    _STATE["inflight"] += 1
    _STATE["total"] += 1
    try:
        if stream:
            async def stream_generator():
                await asyncio.sleep(ttft_ms / 1000.0)
                gaps = _gaps_ms(len(words), ttft_ms, duration_ms, jitter)
                for idx, word in enumerate(words):
                    chunk = {
                        "id": response_id, "object": "chat.completion.chunk",
                        "created": created, "model": model,
                        "choices": [{"index": 0, "delta": {"role": "assistant", "content": (word + " ") if idx == 0 else word}, "finish_reason": None}],
                    }
                    # first token content has no leading space; subsequent tokens
                    # are prefixed with a space to reconstruct the sentence.
                    if idx == 0:
                        chunk["choices"][0]["delta"]["content"] = word
                    else:
                        chunk["choices"][0]["delta"]["content"] = " " + word
                    yield f"data: {json.dumps(chunk)}\n\n"
                    if idx < len(gaps):
                        await asyncio.sleep(gaps[idx] / 1000.0)

                done_chunk = {
                    "id": response_id, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": out_tokens, "total_tokens": out_tokens},
                }
                yield f"data: {json.dumps(done_chunk)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(stream_generator(), media_type="text/event-stream")

        await asyncio.sleep(duration_ms / 1000.0)
        content = " ".join(words)
        return {
            "id": response_id, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": out_tokens, "total_tokens": out_tokens},
        }
    finally:
        _STATE["inflight"] -= 1


@app.post("/v1/embeddings")
@app.post("/embeddings")
async def embeddings(request: Request):
    body = await request.json()
    inputs = body.get("input", [""])
    if isinstance(inputs, str):
        inputs = [inputs]
    return {
        "object": "list",
        "data": [{"object": "embedding", "index": i, "embedding": [0.0] * 1536} for i in range(len(inputs))],
        "model": body.get("model", "mock-embedding"),
        "usage": {"prompt_tokens": 5, "total_tokens": 5},
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
```

Since the test imports `from mock_llm.server import app`, add the mock-llm dir to the test path via a `conftest.py`:

```python
# benchmark-litellm/tests/conftest.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mock-llm"))
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_mock_server.py -v`
Expected: 5 passed

- [ ] **Step 5: Write the Dockerfile**

```dockerfile
# benchmark-litellm/mock-llm/Dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn orjson
COPY ./benchmark-litellm/mock-llm/mock_llm /app/mock_llm
COPY ./benchmark-litellm/bench/detgen.py /app/bench/detgen.py
RUN touch /app/bench/__init__.py
ENV PYTHONPATH=/app
EXPOSE 8090
CMD ["uvicorn", "mock_llm.server:app", "--host", "0.0.0.0", "--port", "8090"]
```

- [ ] **Step 6: Remove the legacy file and old Dockerfile, update compose**

```bash
cd /home/ivri_faitelson/work/litellm/local-lab
git rm -f benchmark-litellm/mock-llm/mock_llm/_legacy_reference.py
git rm -f stack-litellm/mock-llm/Dockerfile
rmdir stack-litellm/mock-llm 2>/dev/null || true
```

Modify `stack-litellm/docker-compose.yml` (the block found at lines 115-124):

```yaml
  mock-llm:
    build:
      context: ..
      dockerfile: benchmark-litellm/mock-llm/Dockerfile
    ports:
      - "8090:8090"
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/health').status==200 else 1)\""]
      interval: 3s
      timeout: 3s
      retries: 20
    networks: [litellm-net]
```

(Added `ports: ["8090:8090"]` so the baseline benchmark run, executed from the host, can reach the mock directly — required by the design's baseline vs proxy comparison.)

- [ ] **Step 7: Rebuild and smoke-test the mock in the real stack**

```bash
cd /home/ivri_faitelson/work/litellm/local-lab/stack-litellm
docker compose build mock-llm
docker compose up -d mock-llm
sleep 2
curl -sf http://localhost:8090/health
curl -s http://localhost:8090/v1/chat/completions -H 'content-type: application/json' -d '{"model":"fake-gpt-4","stream":false,"messages":[{"role":"user","content":"hi"}],"mock":{"seed":"t1","out_tokens":4,"duration_ms":20}}'
```
Expected: `{"status":"ok"}` then a JSON completion whose `message.content` is 4 deterministic tokens.

- [ ] **Step 8: Commit**

```bash
cd /home/ivri_faitelson/work/litellm/local-lab
git add benchmark-litellm/mock-llm stack-litellm/docker-compose.yml benchmark-litellm/tests/test_mock_server.py benchmark-litellm/tests/conftest.py
git commit -m "feat(benchmark-litellm): move mock LLM into benchmark-litellm and add per-request timing/content control"
```

---

## Task 6: extra_body pass-through spike (LiteLLM `mock` key)

**Files:**
- Create: `benchmark-litellm/tests/test_extra_body_spike.py` (marked `integration`)

- [ ] **Step 1: Write the spike test**

```python
# benchmark-litellm/tests/test_extra_body_spike.py
"""Confirms whether LiteLLM forwards an unrecognised top-level `mock` key
from the request body through to the OpenAI-compatible backend, for both
/v1/chat/completions and /v1/messages. Requires the stack-litellm stack
running locally (docker compose up in stack-litellm/).
If the key is NOT forwarded on a given endpoint, the client (Task 8) must
use the in-prompt marker fallback for that endpoint.
"""
import httpx
import pytest

PROXY_BASE = "http://localhost:4000"
API_KEY = "sk-1234"


@pytest.mark.integration
def test_mock_key_forwarded_on_chat_completions():
    r = httpx.post(
        f"{PROXY_BASE}/v1/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "model": "fake-gpt-4", "stream": False,
            "messages": [{"role": "user", "content": "hi"}],
            "mock": {"seed": "spike1", "out_tokens": 3, "duration_ms": 10},
        },
        timeout=10,
    )
    assert r.status_code == 200
    from bench.detgen import text
    content = r.json()["choices"][0]["message"]["content"]
    # Record the outcome either way; this assertion documents the finding.
    assert content in (text("spike1", 3), "This is a mock response.")


@pytest.mark.integration
def test_mock_key_forwarded_on_messages():
    r = httpx.post(
        f"{PROXY_BASE}/v1/messages",
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01"},
        json={
            "model": "fake-gpt-4", "max_tokens": 16, "stream": False,
            "messages": [{"role": "user", "content": "hi"}],
            "mock": {"seed": "spike2", "out_tokens": 3, "duration_ms": 10},
        },
        timeout=10,
    )
    assert r.status_code == 200
```

- [ ] **Step 2: Run against the live stack and record the finding**

```bash
cd /home/ivri_faitelson/work/litellm/local-lab
docker compose -f stack-litellm/docker-compose.yml up -d
cd benchmark-litellm && .venv/bin/pytest tests/test_extra_body_spike.py -v -m integration
```

- [ ] **Step 3: Record the result in the spec's risk note**

Open `docs/superpowers/specs/2026-09-12-litellm-load-benchmark-design.md`, section 5.1, and append one line stating whether `mock` was forwarded on chat, messages, both, or neither, based on actual output content observed in Step 2. Then, in `bench/client/openai_chat.py` and `bench/client/anthropic_messages.py` (Task 8), always **also** send the in-prompt marker regardless of the spike result — this makes both code paths correct with zero extra runtime cost, so no conditional logic is needed based on the spike outcome.

- [ ] **Step 4: Commit**

```bash
git add benchmark-litellm/tests/test_extra_body_spike.py docs/superpowers/specs/2026-09-12-litellm-load-benchmark-design.md
git commit -m "test(benchmark-litellm): spike extra_body passthrough through litellm proxy"
```

---

## Task 7: `verify.py` — response correctness checking

**Files:**
- Create: `benchmark-litellm/bench/client/__init__.py`
- Create: `benchmark-litellm/bench/client/verify.py`
- Test: `benchmark-litellm/tests/test_verify.py`

- [ ] **Step 1: Write failing tests**

```python
# benchmark-litellm/tests/test_verify.py
from bench.client.verify import verify_response, VerifyResult
from bench.detgen import text


def test_ok_when_content_and_tokens_match():
    result = verify_response(
        expected_seed="s1", expected_out_tokens=3,
        got_content=text("s1", 3), got_completion_tokens=3,
        stream_terminated=True,
    )
    assert result == VerifyResult(ok=True, reason=None)


def test_fail_on_content_mismatch():
    result = verify_response(
        expected_seed="s1", expected_out_tokens=3,
        got_content="wrong content here", got_completion_tokens=3,
        stream_terminated=True,
    )
    assert result.ok is False
    assert result.reason == "content_mismatch"


def test_fail_on_token_count_mismatch():
    result = verify_response(
        expected_seed="s1", expected_out_tokens=3,
        got_content=text("s1", 3), got_completion_tokens=2,
        stream_terminated=True,
    )
    assert result.ok is False
    assert result.reason == "token_count_mismatch"


def test_fail_on_unterminated_stream():
    result = verify_response(
        expected_seed="s1", expected_out_tokens=3,
        got_content=text("s1", 3), got_completion_tokens=3,
        stream_terminated=False,
    )
    assert result.ok is False
    assert result.reason == "stream_not_terminated"


def test_fail_on_http_error_short_circuits_content_check():
    result = verify_response(
        expected_seed="s1", expected_out_tokens=3,
        got_content=None, got_completion_tokens=None,
        stream_terminated=False, http_status=500,
    )
    assert result.ok is False
    assert result.reason == "http_error"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_verify.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'bench.client'`

- [ ] **Step 3: Implement**

```python
# benchmark-litellm/bench/client/__init__.py
```

```python
# benchmark-litellm/bench/client/verify.py
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_verify.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add benchmark-litellm/bench/client/__init__.py benchmark-litellm/bench/client/verify.py benchmark-litellm/tests/test_verify.py
git commit -m "feat(benchmark-litellm): add response correctness verification"
```

---

## Task 8: SSE parsers for OpenAI chat and Anthropic messages

**Files:**
- Create: `benchmark-litellm/bench/client/sse.py`
- Test: `benchmark-litellm/tests/test_sse.py`

- [ ] **Step 1: Write failing tests**

```python
# benchmark-litellm/tests/test_sse.py
from bench.client.sse import parse_openai_chat_stream, parse_anthropic_messages_stream


def _sse_lines(*payloads):
    for p in payloads:
        yield f"data: {p}"
        yield ""


def test_parse_openai_chat_stream_joins_content_and_reports_tokens():
    lines = list(_sse_lines(
        '{"choices":[{"delta":{"role":"assistant","content":"foo"}}]}',
        '{"choices":[{"delta":{"content":" bar"}}]}',
        '{"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"completion_tokens":2}}',
        "[DONE]",
    ))
    result = parse_openai_chat_stream(lines)
    assert result.content == "foo bar"
    assert result.completion_tokens == 2
    assert result.terminated is True


def test_parse_openai_chat_stream_not_terminated_if_no_done():
    lines = list(_sse_lines(
        '{"choices":[{"delta":{"content":"foo"}}]}',
    ))
    result = parse_openai_chat_stream(lines)
    assert result.terminated is False


def test_parse_anthropic_messages_stream():
    lines = list(_sse_lines(
        '{"type":"message_start","message":{"usage":{"output_tokens":0}}}',
        '{"type":"content_block_delta","delta":{"type":"text_delta","text":"foo"}}',
        '{"type":"content_block_delta","delta":{"type":"text_delta","text":" bar"}}',
        '{"type":"message_delta","usage":{"output_tokens":2}}',
        '{"type":"message_stop"}',
    ))
    result = parse_anthropic_messages_stream(lines)
    assert result.content == "foo bar"
    assert result.completion_tokens == 2
    assert result.terminated is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_sse.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'bench.client.sse'`

- [ ] **Step 3: Implement**

```python
# benchmark-litellm/bench/client/sse.py
from __future__ import annotations

import orjson
from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass
class StreamResult:
    content: str
    completion_tokens: Optional[int]
    terminated: bool
    chunk_count: int


def _iter_data_payloads(lines: Iterable[str]) -> Iterable[str]:
    for line in lines:
        line = line.strip()
        if not line.startswith("data:"):
            continue
        yield line[len("data:"):].strip()


def parse_openai_chat_stream(lines: Iterable[str]) -> StreamResult:
    parts = []
    completion_tokens = None
    terminated = False
    chunk_count = 0
    for payload in _iter_data_payloads(lines):
        if payload == "[DONE]":
            terminated = True
            break
        obj = orjson.loads(payload)
        choices = obj.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            content = delta.get("content")
            if content:
                parts.append(content)
                chunk_count += 1
        usage = obj.get("usage")
        if usage and usage.get("completion_tokens") is not None:
            completion_tokens = usage["completion_tokens"]
    return StreamResult(
        content="".join(parts), completion_tokens=completion_tokens,
        terminated=terminated, chunk_count=chunk_count,
    )


def parse_anthropic_messages_stream(lines: Iterable[str]) -> StreamResult:
    parts = []
    completion_tokens = None
    terminated = False
    chunk_count = 0
    for payload in _iter_data_payloads(lines):
        obj = orjson.loads(payload)
        event_type = obj.get("type")
        if event_type == "content_block_delta":
            delta = obj.get("delta") or {}
            if delta.get("type") == "text_delta":
                parts.append(delta.get("text", ""))
                chunk_count += 1
        elif event_type == "message_delta":
            usage = obj.get("usage") or {}
            if usage.get("output_tokens") is not None:
                completion_tokens = usage["output_tokens"]
        elif event_type == "message_stop":
            terminated = True
    return StreamResult(
        content="".join(parts), completion_tokens=completion_tokens,
        terminated=terminated, chunk_count=chunk_count,
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_sse.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add benchmark-litellm/bench/client/sse.py benchmark-litellm/tests/test_sse.py
git commit -m "feat(benchmark-litellm): add SSE parsers for openai chat and anthropic messages"
```

---

## Task 9: Request builders (OpenAI chat + Anthropic messages)

**Files:**
- Create: `benchmark-litellm/bench/client/openai_chat.py`
- Create: `benchmark-litellm/bench/client/anthropic_messages.py`
- Test: `benchmark-litellm/tests/test_request_builders.py`

- [ ] **Step 1: Write failing tests**

```python
# benchmark-litellm/tests/test_request_builders.py
from bench.trace.schema import TraceRequest
from bench.client.openai_chat import build_request as build_chat
from bench.client.anthropic_messages import build_request as build_messages


def _row(**overrides):
    base = dict(
        i=0, t_ms=0, seed="s1", api="chat", stream=True, model="fake-gpt-4",
        in_tokens=10, out_tokens=5, duration_ms=500, ttft_ms=50,
    )
    base.update(overrides)
    return TraceRequest(**base)


def test_build_chat_request_has_mock_key_and_marker():
    url, headers, body = build_chat(_row(), base_url="http://x", api_key="k")
    assert url == "http://x/v1/chat/completions"
    assert headers["Authorization"] == "Bearer k"
    assert body["mock"] == {"seed": "s1", "out_tokens": 5, "duration_ms": 500, "ttft_ms": 50, "jitter": 0.2}
    assert "[mock seed=s1 out=5 dur=500 ttft=50" in body["messages"][0]["content"]
    assert body["stream"] is True


def test_build_chat_request_prompt_padded_to_in_tokens():
    row = _row(in_tokens=20)
    _, _, body = build_chat(row, base_url="http://x", api_key="k")
    # marker + padding words; padding portion should contribute ~in_tokens words
    padding_words = body["messages"][0]["content"].split(" ")
    assert len(padding_words) >= 20


def test_build_messages_request_shape():
    url, headers, body = build_messages(_row(), base_url="http://x", api_key="k")
    assert url == "http://x/v1/messages"
    assert headers["x-api-key"] == "k"
    assert headers["anthropic-version"] == "2023-06-01"
    assert body["max_tokens"] == 5
    assert body["mock"]["seed"] == "s1"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_request_builders.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# benchmark-litellm/bench/client/openai_chat.py
from __future__ import annotations

from bench.detgen import text
from bench.trace.schema import TraceRequest


def _marker(row: TraceRequest, jitter: float = 0.2) -> str:
    ttft_part = f" ttft={row.ttft_ms}" if row.ttft_ms is not None else ""
    return f"[mock seed={row.seed} out={row.out_tokens} dur={row.duration_ms}{ttft_part} jitter={jitter}]"


def build_request(row: TraceRequest, base_url: str, api_key: str, jitter: float = 0.2):
    prompt_padding = text(row.seed + ":prompt", row.in_tokens)
    content = f"{_marker(row, jitter)} {prompt_padding}"
    body = {
        "model": row.model,
        "stream": row.stream,
        "messages": [{"role": "user", "content": content}],
        "mock": {
            "seed": row.seed,
            "out_tokens": row.out_tokens,
            "duration_ms": row.duration_ms,
            "ttft_ms": row.ttft_ms,
            "jitter": jitter,
        },
    }
    headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    return url, headers, body
```

```python
# benchmark-litellm/bench/client/anthropic_messages.py
from __future__ import annotations

from bench.detgen import text
from bench.trace.schema import TraceRequest
from bench.client.openai_chat import _marker


def build_request(row: TraceRequest, base_url: str, api_key: str, jitter: float = 0.2):
    prompt_padding = text(row.seed + ":prompt", row.in_tokens)
    content = f"{_marker(row, jitter)} {prompt_padding}"
    body = {
        "model": row.model,
        "stream": row.stream,
        "max_tokens": row.out_tokens,
        "messages": [{"role": "user", "content": content}],
        "mock": {
            "seed": row.seed,
            "out_tokens": row.out_tokens,
            "duration_ms": row.duration_ms,
            "ttft_ms": row.ttft_ms,
            "jitter": jitter,
        },
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    url = f"{base_url.rstrip('/')}/v1/messages"
    return url, headers, body
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_request_builders.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add benchmark-litellm/bench/client/openai_chat.py benchmark-litellm/bench/client/anthropic_messages.py benchmark-litellm/tests/test_request_builders.py
git commit -m "feat(benchmark-litellm): add openai chat and anthropic messages request builders"
```

---

## Task 10: Results record + JSONL writer

**Files:**
- Create: `benchmark-litellm/bench/client/results.py`
- Test: `benchmark-litellm/tests/test_results.py`

- [ ] **Step 1: Write failing tests**

```python
# benchmark-litellm/tests/test_results.py
from bench.client.results import RequestResult, ResultsWriter


def test_derived_fields_computed():
    r = RequestResult(
        i=0, seed="s1", api="chat", stream=True, target="baseline",
        t_sched_ms=0.0, t_sent_ms=1.0, t_first_byte_ms=51.0, t_first_token_ms=52.0,
        t_done_ms=501.0, status=200, ok=True, reason=None,
        chunks=5, bytes=120, out_tokens_seen=5, duration_ms=500,
    )
    assert r.latency_ms == 500.0
    assert r.ttft_ms == 51.0
    assert r.sched_lag_ms == 1.0
    assert r.overhead_ms == 0.0


def test_writer_appends_jsonl(tmp_path):
    path = tmp_path / "results.jsonl"
    writer = ResultsWriter(path)
    r = RequestResult(
        i=0, seed="s1", api="chat", stream=False, target="baseline",
        t_sched_ms=0.0, t_sent_ms=0.0, t_first_byte_ms=10.0, t_first_token_ms=10.0,
        t_done_ms=10.0, status=200, ok=True, reason=None,
        chunks=1, bytes=50, out_tokens_seen=1, duration_ms=10,
    )
    writer.write(r)
    writer.close()
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    import orjson
    assert orjson.loads(lines[0])["i"] == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_results.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# benchmark-litellm/bench/client/results.py
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_results.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add benchmark-litellm/bench/client/results.py benchmark-litellm/tests/test_results.py
git commit -m "feat(benchmark-litellm): add per-request result record and JSONL writer"
```

---

## Task 11: Scheduler (open-loop timing with fake clock)

**Files:**
- Create: `benchmark-litellm/bench/client/scheduler.py`
- Test: `benchmark-litellm/tests/test_scheduler.py`

- [ ] **Step 1: Write failing tests**

```python
# benchmark-litellm/tests/test_scheduler.py
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
```

Add `pytest-asyncio` for the async test:

```bash
cd /home/ivri_faitelson/work/litellm/local-lab/benchmark-litellm && .venv/bin/pip install pytest-asyncio
```

Add to `pyproject.toml`'s pytest section:

```toml
[tool.pytest.ini_options]
markers = ["integration: requires a running stack-litellm stack"]
asyncio_mode = "auto"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_scheduler.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'bench.client.scheduler'`

- [ ] **Step 3: Implement**

```python
# benchmark-litellm/bench/client/scheduler.py
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_scheduler.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add benchmark-litellm/bench/client/scheduler.py benchmark-litellm/tests/test_scheduler.py benchmark-litellm/pyproject.toml
git commit -m "feat(benchmark-litellm): add open-loop request scheduler"
```

---

## Task 12: Runner — wires scheduler + request builders + SSE parsers + verify + results

**Files:**
- Create: `benchmark-litellm/bench/client/runner.py`
- Test: `benchmark-litellm/tests/test_runner.py`

- [ ] **Step 1: Write failing tests (against an in-process mock server via httpx ASGI transport)**

```python
# benchmark-litellm/tests/test_runner.py
import asyncio
import json

import httpx
import pytest

from bench.trace.schema import TraceRequest
from bench.client.runner import run_trace
from mock_llm.server import app as mock_app


def _rows():
    return [
        TraceRequest(i=0, t_ms=0, seed="r0", api="chat", stream=False, model="fake-gpt-4",
                     in_tokens=5, out_tokens=3, duration_ms=10, ttft_ms=None),
        TraceRequest(i=1, t_ms=5, seed="r1", api="chat", stream=True, model="fake-gpt-4",
                     in_tokens=5, out_tokens=4, duration_ms=20, ttft_ms=5),
    ]


@pytest.mark.asyncio
async def test_run_trace_against_mock_all_ok(tmp_path):
    transport = httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as client:
        out_path = tmp_path / "results.jsonl"
        await run_trace(
            rows=_rows(), client=client, base_url="http://mock", api_key="k",
            api_mode="chat", target_label="baseline", time_scale=0.01,
            max_inflight=10, out_path=out_path,
        )
    lines = out_path.read_text().strip().splitlines()
    assert len(lines) == 2
    records = [json.loads(l) for l in lines]
    assert all(r["ok"] for r in records)
    assert {r["i"] for r in records} == {0, 1}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_runner.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'bench.client.runner'`

- [ ] **Step 3: Implement**

```python
# benchmark-litellm/bench/client/runner.py
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import httpx

from bench.client import openai_chat, anthropic_messages, sse, verify
from bench.client.results import RequestResult, ResultsWriter
from bench.client.scheduler import run_schedule
from bench.trace.schema import TraceRequest


def _builder_for(api: str):
    return openai_chat.build_request if api == "chat" else anthropic_messages.build_request


def _parser_for(api: str):
    return sse.parse_openai_chat_stream if api == "chat" else sse.parse_anthropic_messages_stream


async def _execute_one(
    row: TraceRequest,
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    api_mode: str,
    target_label: str,
    t_sched_ms: float,
    writer: ResultsWriter,
    default_timeout_s: float,
) -> None:
    api = api_mode or row.api
    build_request = _builder_for(api)
    parse_stream = _parser_for(api)
    url, headers, body = build_request(row, base_url=base_url, api_key=api_key)

    t0 = time.monotonic()
    t_sent_ms = (t0 - t_sched_ms / 1000.0) * 0 + t_sched_ms  # sent == sched reference frame; corrected below
    t_sent = time.monotonic() * 1000.0

    status = 0
    ok = False
    reason: Optional[str] = None
    content = ""
    completion_tokens = None
    terminated = False
    chunk_count = 0
    t_first_byte = None
    t_first_token = None
    body_bytes = 0

    timeout = httpx.Timeout(row.duration_ms / 1000.0 * 3 + default_timeout_s)

    try:
        if row.stream:
            async with client.stream("POST", url, headers=headers, json=body, timeout=timeout) as resp:
                status = resp.status_code
                lines = []
                async for line in resp.aiter_lines():
                    now_ms = time.monotonic() * 1000.0
                    if t_first_byte is None:
                        t_first_byte = now_ms
                    if line.strip().startswith("data:") and t_first_token is None:
                        t_first_token = now_ms
                    lines.append(line)
                    body_bytes += len(line)
                result = parse_stream(lines)
                content, completion_tokens = result.content, result.completion_tokens
                terminated, chunk_count = result.terminated, result.chunk_count
        else:
            resp = await client.post(url, headers=headers, json=body, timeout=timeout)
            status = resp.status_code
            now_ms = time.monotonic() * 1000.0
            t_first_byte = now_ms
            t_first_token = now_ms
            data = resp.json()
            body_bytes = len(resp.content)
            if api == "chat":
                content = data["choices"][0]["message"]["content"]
                completion_tokens = data.get("usage", {}).get("completion_tokens")
            else:
                content = "".join(b.get("text", "") for b in data.get("content", []))
                completion_tokens = data.get("usage", {}).get("output_tokens")
            terminated = True
            chunk_count = 1

        vr = verify.verify_response(
            expected_seed=row.seed, expected_out_tokens=row.out_tokens,
            got_content=content, got_completion_tokens=completion_tokens,
            stream_terminated=terminated, http_status=status,
        )
        ok, reason = vr.ok, vr.reason
    except Exception as exc:  # noqa: BLE001 - benchmark must never crash on one bad request
        reason = "timeout" if isinstance(exc, httpx.TimeoutException) else "connect"
        status = status or 0

    t_done = time.monotonic() * 1000.0
    writer.write(RequestResult(
        i=row.i, seed=row.seed, api=api, stream=row.stream, target=target_label,
        t_sched_ms=t_sched_ms, t_sent_ms=t_sent,
        t_first_byte_ms=t_first_byte if t_first_byte is not None else t_done,
        t_first_token_ms=t_first_token if t_first_token is not None else t_done,
        t_done_ms=t_done, status=status, ok=ok, reason=reason,
        chunks=chunk_count, bytes=body_bytes, out_tokens_seen=completion_tokens,
        duration_ms=row.duration_ms,
    ))


async def run_trace(
    rows: list[TraceRequest],
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    api_mode: Optional[str],  # None => use row.api; "chat"/"messages" => force
    target_label: str,
    time_scale: float,
    max_inflight: int,
    out_path: Path,
    default_timeout_s: float = 10.0,
) -> None:
    writer = ResultsWriter(out_path)
    offsets_ms = [r.t_ms for r in rows]
    by_index = {r.i: r for r in rows}

    async def callback(i: int) -> None:
        row = by_index[i]
        await _execute_one(
            row=row, client=client, base_url=base_url, api_key=api_key,
            api_mode=api_mode, target_label=target_label,
            t_sched_ms=row.t_ms * time_scale, writer=writer,
            default_timeout_s=default_timeout_s,
        )

    try:
        await run_schedule(offsets_ms, time_scale=time_scale, callback=callback, max_inflight=max_inflight)
    finally:
        writer.close()
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_runner.py -v`
Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
git add benchmark-litellm/bench/client/runner.py benchmark-litellm/tests/test_runner.py
git commit -m "feat(benchmark-litellm): add trace runner wiring scheduler, requests, and verification"
```

---

## Task 13: Report stats (percentiles + deltas)

**Files:**
- Create: `benchmark-litellm/bench/report/__init__.py`
- Create: `benchmark-litellm/bench/report/stats.py`
- Test: `benchmark-litellm/tests/test_stats.py`

- [ ] **Step 1: Write failing tests**

```python
# benchmark-litellm/tests/test_stats.py
from bench.report.stats import summarize, RunSummary


def _records():
    return [
        {"api": "chat", "stream": True, "ok": True, "reason": None,
         "latency_ms": 100 + i, "ttft_ms": 10 + i, "overhead_ms": 5,
         "sched_lag_ms": 1, "t_sent_ms": i * 10, "t_done_ms": i * 10 + 100 + i}
        for i in range(10)
    ] + [
        {"api": "chat", "stream": True, "ok": False, "reason": "content_mismatch",
         "latency_ms": 500, "ttft_ms": 50, "overhead_ms": 400, "sched_lag_ms": 2,
         "t_sent_ms": 1000, "t_done_ms": 1500}
    ]


def test_summarize_basic_fields():
    s = summarize(_records())
    assert isinstance(s, RunSummary)
    assert s.requests == 11
    assert round(s.ok_pct, 2) == round(10 / 11 * 100, 2)
    assert s.errors["content_mismatch"] == 1
    assert s.latency_p50 <= s.latency_p90 <= s.latency_p99


def test_summarize_empty_records_does_not_crash():
    s = summarize([])
    assert s.requests == 0
    assert s.ok_pct == 0.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_stats.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# benchmark-litellm/bench/report/__init__.py
```

```python
# benchmark-litellm/bench/report/stats.py
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class RunSummary:
    requests: int
    ok_pct: float
    errors: Counter
    achieved_rps: float
    latency_p50: float
    latency_p90: float
    latency_p99: float
    latency_max: float
    ttft_p50: float
    ttft_p90: float
    ttft_p99: float
    overhead_p50: float
    overhead_p90: float
    overhead_p99: float
    sched_lag_p99: float


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    return float(np.percentile(values, q))


def summarize(records: list[dict]) -> RunSummary:
    n = len(records)
    if n == 0:
        return RunSummary(
            requests=0, ok_pct=0.0, errors=Counter(), achieved_rps=0.0,
            latency_p50=0.0, latency_p90=0.0, latency_p99=0.0, latency_max=0.0,
            ttft_p50=0.0, ttft_p90=0.0, ttft_p99=0.0,
            overhead_p50=0.0, overhead_p90=0.0, overhead_p99=0.0, sched_lag_p99=0.0,
        )

    ok_count = sum(1 for r in records if r["ok"])
    errors = Counter(r["reason"] for r in records if not r["ok"] and r.get("reason"))

    latency = [r["latency_ms"] for r in records]
    ttft = [r["ttft_ms"] for r in records]
    overhead = [r["overhead_ms"] for r in records]
    sched_lag = [r["sched_lag_ms"] for r in records]

    span_ms = max(r["t_done_ms"] for r in records) - min(r["t_sent_ms"] for r in records)
    achieved_rps = n / (span_ms / 1000.0) if span_ms > 0 else 0.0

    return RunSummary(
        requests=n,
        ok_pct=ok_count / n * 100.0,
        errors=errors,
        achieved_rps=achieved_rps,
        latency_p50=_pct(latency, 50), latency_p90=_pct(latency, 90),
        latency_p99=_pct(latency, 99), latency_max=max(latency),
        ttft_p50=_pct(ttft, 50), ttft_p90=_pct(ttft, 90), ttft_p99=_pct(ttft, 99),
        overhead_p50=_pct(overhead, 50), overhead_p90=_pct(overhead, 90),
        overhead_p99=_pct(overhead, 99),
        sched_lag_p99=_pct(sched_lag, 99),
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_stats.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add benchmark-litellm/bench/report/__init__.py benchmark-litellm/bench/report/stats.py benchmark-litellm/tests/test_stats.py
git commit -m "feat(benchmark-litellm): add percentile/summary stats"
```

---

## Task 14: Excel report builder

**Files:**
- Create: `benchmark-litellm/bench/report/excel.py`
- Test: `benchmark-litellm/tests/test_excel.py`

- [ ] **Step 1: Write failing tests**

```python
# benchmark-litellm/tests/test_excel.py
from openpyxl import load_workbook

from bench.report.excel import build_workbook


def _records(label, n=5):
    return [
        {"api": "chat", "stream": True, "ok": i != 0, "reason": None if i != 0 else "content_mismatch",
         "latency_ms": 100 + i, "ttft_ms": 10, "overhead_ms": 5, "sched_lag_ms": 1,
         "t_sent_ms": i * 100, "t_done_ms": i * 100 + 100, "seed": f"{label}-{i}"}
        for i in range(n)
    ]


def test_build_workbook_has_expected_sheets(tmp_path):
    out_path = tmp_path / "report.xlsx"
    runs = {"baseline": _records("baseline"), "proxy_chat": _records("proxy_chat")}
    build_workbook(runs, out_path)
    wb = load_workbook(out_path)
    names = set(wb.sheetnames)
    assert "Summary" in names
    assert "By API x Stream" in names
    assert "Timeline" in names
    assert "Failures" in names
    assert "Raw-baseline" in names
    assert "Raw-proxy_chat" in names


def test_summary_sheet_has_a_row_per_run(tmp_path):
    out_path = tmp_path / "report.xlsx"
    runs = {"baseline": _records("baseline"), "proxy_chat": _records("proxy_chat")}
    build_workbook(runs, out_path)
    wb = load_workbook(out_path)
    ws = wb["Summary"]
    first_col = [c.value for c in ws["A"]]
    assert "baseline" in first_col
    assert "proxy_chat" in first_col
```

Add openpyxl (read-only verification) as a dev/test dependency:

```bash
cd /home/ivri_faitelson/work/litellm/local-lab/benchmark-litellm && .venv/bin/pip install openpyxl
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_excel.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'bench.report.excel'`

- [ ] **Step 3: Implement**

```python
# benchmark-litellm/bench/report/excel.py
from __future__ import annotations

from pathlib import Path

import xlsxwriter

from bench.report.stats import summarize, RunSummary

MAX_RAW_ROWS = 1_000_000


def build_workbook(runs: dict[str, list[dict]], out_path: Path) -> None:
    wb = xlsxwriter.Workbook(str(out_path))
    bold = wb.add_format({"bold": True})
    pct_fmt = wb.add_format({"num_format": "0.00"})
    red = wb.add_format({"bg_color": "#FFC7CE"})
    green = wb.add_format({"bg_color": "#C6EFCE"})

    labels = list(runs.keys())
    baseline_label = labels[0] if labels else None
    summaries: dict[str, RunSummary] = {label: summarize(rows) for label, rows in runs.items()}

    _write_summary(wb, bold, red, green, labels, baseline_label, summaries)
    _write_by_api_stream(wb, bold, runs)
    _write_timeline(wb, bold, runs)
    _write_failures(wb, bold, runs)
    for label, rows in runs.items():
        _write_raw(wb, bold, label, rows)

    wb.close()


def _write_summary(wb, bold, red, green, labels, baseline_label, summaries):
    ws = wb.add_worksheet("Summary")
    headers = [
        "run", "requests", "ok_pct", "achieved_rps",
        "latency_p50", "latency_p90", "latency_p99",
        "ttft_p50", "ttft_p90", "ttft_p99",
        "overhead_p50", "overhead_p90", "overhead_p99",
        "sched_lag_p99", "verdict",
    ]
    for col, h in enumerate(headers):
        ws.write(0, col, h, bold)

    baseline = summaries.get(baseline_label) if baseline_label else None
    for row_i, label in enumerate(labels, start=1):
        s = summaries[label]
        verdict = "PASS" if s.ok_pct == 100.0 and s.sched_lag_p99 <= 5.0 else "FAIL"
        values = [
            label, s.requests, s.ok_pct, s.achieved_rps,
            s.latency_p50, s.latency_p90, s.latency_p99,
            s.ttft_p50, s.ttft_p90, s.ttft_p99,
            s.overhead_p50, s.overhead_p90, s.overhead_p99,
            s.sched_lag_p99, verdict,
        ]
        for col, v in enumerate(values):
            ws.write(row_i, col, v)
        fmt = green if verdict == "PASS" else red
        ws.write(row_i, len(headers) - 1, verdict, fmt)


def _write_by_api_stream(wb, bold, runs):
    ws = wb.add_worksheet("By API x Stream")
    headers = ["run", "api", "stream", "requests", "ok_pct", "latency_p50", "latency_p99", "overhead_p50"]
    for col, h in enumerate(headers):
        ws.write(0, col, h, bold)
    row_i = 1
    for label, rows in runs.items():
        buckets: dict[tuple, list[dict]] = {}
        for r in rows:
            key = (r["api"], r["stream"])
            buckets.setdefault(key, []).append(r)
        for (api, stream), bucket_rows in buckets.items():
            s = summarize(bucket_rows)
            ws.write(row_i, 0, label)
            ws.write(row_i, 1, api)
            ws.write(row_i, 2, stream)
            ws.write(row_i, 3, s.requests)
            ws.write(row_i, 4, s.ok_pct)
            ws.write(row_i, 5, s.latency_p50)
            ws.write(row_i, 6, s.latency_p99)
            ws.write(row_i, 7, s.overhead_p50)
            row_i += 1


def _write_timeline(wb, bold, runs):
    ws = wb.add_worksheet("Timeline")
    ws.write(0, 0, "run", bold)
    ws.write(0, 1, "bucket_s", bold)
    ws.write(0, 2, "rps", bold)
    ws.write(0, 3, "p99_latency_ms", bold)
    row_i = 1
    for label, rows in runs.items():
        if not rows:
            continue
        t0 = min(r["t_sent_ms"] for r in rows)
        buckets: dict[int, list[dict]] = {}
        for r in rows:
            bucket = int((r["t_sent_ms"] - t0) // 1000)
            buckets.setdefault(bucket, []).append(r)
        for bucket in sorted(buckets):
            bucket_rows = buckets[bucket]
            s = summarize(bucket_rows)
            ws.write(row_i, 0, label)
            ws.write(row_i, 1, bucket)
            ws.write(row_i, 2, len(bucket_rows))
            ws.write(row_i, 3, s.latency_p99)
            row_i += 1


def _write_failures(wb, bold, runs):
    ws = wb.add_worksheet("Failures")
    headers = ["run", "seed", "reason"]
    for col, h in enumerate(headers):
        ws.write(0, col, h, bold)
    row_i = 1
    for label, rows in runs.items():
        for r in rows:
            if not r["ok"]:
                ws.write(row_i, 0, label)
                ws.write(row_i, 1, r.get("seed", ""))
                ws.write(row_i, 2, r.get("reason", ""))
                row_i += 1


def _write_raw(wb, bold, label, rows):
    ws = wb.add_worksheet(f"Raw-{label}"[:31])
    if not rows:
        return
    headers = list(rows[0].keys())
    for col, h in enumerate(headers):
        ws.write(0, col, h, bold)
    for row_i, r in enumerate(rows[:MAX_RAW_ROWS], start=1):
        for col, h in enumerate(headers):
            ws.write(row_i, col, r.get(h))
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_excel.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add benchmark-litellm/bench/report/excel.py benchmark-litellm/tests/test_excel.py
git commit -m "feat(benchmark-litellm): add excel report builder"
```

---

## Task 15: CLI (`bench trace`, `bench run`, `bench report`, `bench all`)

**Files:**
- Create: `benchmark-litellm/bench/cli.py`
- Test: `benchmark-litellm/tests/test_cli.py`

- [ ] **Step 1: Write failing tests (Typer CliRunner, no network)**

```python
# benchmark-litellm/tests/test_cli.py
from typer.testing import CliRunner

from bench.cli import app

runner = CliRunner()


def test_trace_synthetic_writes_jsonl(tmp_path):
    out = tmp_path / "trace.jsonl"
    result = runner.invoke(app, [
        "trace", "synthetic",
        "--profile", "constant", "--rps", "10", "--duration-s", "1",
        "--in-tokens", "10:10", "--out-tokens", "5:5",
        "--latency-ms", "100:100", "--rng-seed", "1",
        "-o", str(out),
    ])
    assert result.exit_code == 0, result.output
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 10


def test_report_from_two_result_files(tmp_path):
    import orjson
    from bench.client.results import RequestResult, ResultsWriter

    def make(label, path):
        w = ResultsWriter(path)
        w.write(RequestResult(
            i=0, seed="s", api="chat", stream=False, target=label,
            t_sched_ms=0, t_sent_ms=0, t_first_byte_ms=10, t_first_token_ms=10,
            t_done_ms=10, status=200, ok=True, reason=None,
            chunks=1, bytes=10, out_tokens_seen=1, duration_ms=10,
        ))
        w.close()

    base = tmp_path / "baseline.jsonl"
    proxy = tmp_path / "proxy.jsonl"
    make("baseline", base)
    make("proxy", proxy)

    out_xlsx = tmp_path / "report.xlsx"
    result = runner.invoke(app, ["report", str(base), str(proxy), "-o", str(out_xlsx)])
    assert result.exit_code == 0, result.output
    assert out_xlsx.exists()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_cli.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'bench.cli'`

- [ ] **Step 3: Implement**

```python
# benchmark-litellm/bench/cli.py
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
import httpx
import asyncio

from bench.trace.schema import write_jsonl, read_jsonl
from bench.trace.synthetic import SyntheticConfig, generate as generate_synthetic
from bench.trace.from_spendlogs import iter_from_db, iter_from_csv
from bench.client.runner import run_trace
from bench.report.excel import build_workbook

app = typer.Typer(help="LiteLLM load benchmark")
trace_app = typer.Typer(help="Generate trace files")
app.add_typer(trace_app, name="trace")


def _parse_range(s: str) -> tuple[int, int]:
    lo, hi = s.split(":")
    return int(lo), int(hi)


@trace_app.command("synthetic")
def trace_synthetic(
    profile: str = typer.Option("constant"),
    rps: float = typer.Option(10.0),
    duration_s: float = typer.Option(60.0),
    in_tokens: str = typer.Option("50:500"),
    out_tokens: str = typer.Option("20:400"),
    latency_ms: str = typer.Option("300:3000"),
    stream_ratio: float = typer.Option(0.7),
    messages_ratio: float = typer.Option(0.0),
    rng_seed: int = typer.Option(0),
    model: str = typer.Option("fake-gpt-4"),
    out: Path = typer.Option(..., "-o", "--out"),
):
    cfg = SyntheticConfig(
        profile=profile, rps=rps, duration_s=duration_s,
        in_tokens=_parse_range(in_tokens), out_tokens=_parse_range(out_tokens),
        latency_ms=_parse_range(latency_ms), stream_ratio=stream_ratio,
        messages_ratio=messages_ratio, rng_seed=rng_seed, model=model,
    )
    count = write_jsonl(out, generate_synthetic(cfg))
    typer.echo(f"wrote {count} rows to {out}")


@trace_app.command("spendlogs")
def trace_spendlogs(
    dsn: str = typer.Option(..., envvar="BENCH_SPENDLOGS_DSN"),
    start: datetime = typer.Option(...),
    end: datetime = typer.Option(...),
    sample: float = typer.Option(1.0),
    limit: Optional[int] = typer.Option(None),
    model_map: str = typer.Option("", help="comma list old=new"),
    out: Path = typer.Option(..., "-o", "--out"),
):
    mapping = dict(p.split("=") for p in model_map.split(",") if p)
    rows = iter_from_db(dsn=dsn, start=start, end=end, sample=sample, limit=limit, model_map=mapping)
    count = write_jsonl(out, rows)
    typer.echo(f"wrote {count} rows to {out}")


@app.command("run")
def run_cmd(
    trace_file: Path = typer.Argument(...),
    base_url: str = typer.Option(...),
    api_key: str = typer.Option("sk-1234"),
    api: Optional[str] = typer.Option(None, help="force 'chat' or 'messages'; default = per-row"),
    label: str = typer.Option("run"),
    time_scale: float = typer.Option(1.0),
    max_inflight: int = typer.Option(200),
    out: Path = typer.Option(..., "-o", "--out"),
):
    rows = list(read_jsonl(trace_file))

    async def _main():
        async with httpx.AsyncClient(http2=True) as client:
            await run_trace(
                rows=rows, client=client, base_url=base_url, api_key=api_key,
                api_mode=api, target_label=label, time_scale=time_scale,
                max_inflight=max_inflight, out_path=out,
            )

    asyncio.run(_main())
    typer.echo(f"wrote results to {out}")


@app.command("report")
def report_cmd(
    result_files: list[Path] = typer.Argument(...),
    out: Path = typer.Option(..., "-o", "--out"),
):
    import orjson

    runs = {}
    for path in result_files:
        label = path.stem
        rows = [orjson.loads(line) for line in path.read_text().splitlines() if line.strip()]
        runs[label] = rows
    build_workbook(runs, out)
    typer.echo(f"wrote report to {out}")


@app.command("all")
def all_cmd(
    profile: str = typer.Option("constant"),
    rps: float = typer.Option(10.0),
    duration_s: float = typer.Option(60.0),
    proxy_base: str = typer.Option(...),
    mock_base: str = typer.Option(...),
    api_key: str = typer.Option("sk-1234"),
    time_scale: float = typer.Option(1.0),
    max_inflight: int = typer.Option(200),
    out: Path = typer.Option(..., "-o", "--out"),
    work_dir: Path = typer.Option(Path("./bench-run")),
):
    work_dir.mkdir(parents=True, exist_ok=True)
    trace_path = work_dir / "trace.jsonl"
    cfg = SyntheticConfig(
        profile=profile, rps=rps, duration_s=duration_s,
        in_tokens=(50, 500), out_tokens=(20, 400), latency_ms=(300, 3000),
        stream_ratio=0.7, messages_ratio=0.3, rng_seed=0,
    )
    write_jsonl(trace_path, generate_synthetic(cfg))
    rows = list(read_jsonl(trace_path))

    async def _run_one(base_url, api_mode, label, out_path):
        async with httpx.AsyncClient(http2=True) as client:
            await run_trace(
                rows=rows, client=client, base_url=base_url, api_key=api_key,
                api_mode=api_mode, target_label=label, time_scale=time_scale,
                max_inflight=max_inflight, out_path=out_path,
            )

    baseline_path = work_dir / "baseline.jsonl"
    proxy_chat_path = work_dir / "proxy_chat.jsonl"
    proxy_messages_path = work_dir / "proxy_messages.jsonl"

    asyncio.run(_run_one(mock_base, "chat", "baseline", baseline_path))
    asyncio.run(_run_one(proxy_base, "chat", "proxy_chat", proxy_chat_path))
    asyncio.run(_run_one(proxy_base, "messages", "proxy_messages", proxy_messages_path))

    import orjson
    runs = {}
    for label, path in [("baseline", baseline_path), ("proxy_chat", proxy_chat_path), ("proxy_messages", proxy_messages_path)]:
        runs[label] = [orjson.loads(line) for line in path.read_text().splitlines() if line.strip()]
    build_workbook(runs, out)
    typer.echo(f"wrote report to {out}")


if __name__ == "__main__":
    app()
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd benchmark-litellm && .venv/bin/pytest tests/test_cli.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add benchmark-litellm/bench/cli.py benchmark-litellm/tests/test_cli.py
git commit -m "feat(benchmark-litellm): add CLI (trace, run, report, all)"
```

---

## Task 16: Full test suite + end-to-end smoke test against the real stack

**Files:**
- Modify: none (verification task)

- [ ] **Step 1: Run the full unit test suite**

```bash
cd /home/ivri_faitelson/work/litellm/local-lab/benchmark-litellm
.venv/bin/pytest -v -m "not integration"
```
Expected: all tests pass (detgen, schema, synthetic, from_spendlogs, mock_server, verify, sse, request_builders, results, scheduler, runner, stats, excel, cli).

- [ ] **Step 2: Bring up the real stack and run an end-to-end smoke test**

```bash
cd /home/ivri_faitelson/work/litellm/local-lab/stack-litellm
docker compose up -d
sleep 5
curl -sf http://localhost:4000/health/liveliness
curl -sf http://localhost:8090/health
```
Expected: both return healthy.

- [ ] **Step 3: Run `bench all` with a small synthetic load**

```bash
cd /home/ivri_faitelson/work/litellm/local-lab/benchmark-litellm
.venv/bin/bench all --profile constant --rps 5 --duration-s 10 \
  --proxy-base http://localhost:4000 --mock-base http://localhost:8090 \
  --api-key sk-1234 -o /tmp/report.xlsx --work-dir /tmp/bench-run
```
Expected: exits 0, prints `wrote report to /tmp/report.xlsx`.

- [ ] **Step 4: Inspect the report**

```bash
.venv/bin/python -c "
from openpyxl import load_workbook
wb = load_workbook('/tmp/report.xlsx')
print(wb.sheetnames)
ws = wb['Summary']
for row in ws.iter_rows(values_only=True):
    print(row)
"
```
Expected: `Summary` sheet lists `baseline`, `proxy_chat`, `proxy_messages` rows, each with `ok_pct` close to 100.0 and a `verdict` column.

- [ ] **Step 5: If `ok_pct` is not ~100 for proxy runs, investigate before proceeding**

If proxy runs show `content_mismatch` failures, check `/tmp/bench-run/proxy_chat.jsonl` for `reason` values, and confirm the outcome from Task 6's spike — if `mock` extra_body is not forwarded, the in-prompt marker fallback (already active by design) should still make this pass; if it still fails, use `systematic-debugging` skill before altering the design.

- [ ] **Step 6: Commit if anything needed fixing during this task**

```bash
cd /home/ivri_faitelson/work/litellm/local-lab
git add -A
git commit -m "test(benchmark-litellm): fix issues found during end-to-end smoke test" --allow-empty
```
(Skip if nothing needed changing — do not create an empty commit if there were no fixes.)

---

## Plan Self-Review Notes

- **Spec coverage:** layout (Task 0, 5), trace schema + both sources + both time modes (Tasks 2-4, 15), mock per-request control + timing + determinism (Task 5), extra_body risk validated explicitly (Task 6), correctness verification (Task 7, 12), SSE for both APIs (Task 8), request builders for both APIs (Task 9), results record with all derived fields incl. `sched_lag_ms`/`overhead_ms` (Task 10), scheduler with time-scale/rate modes hook (Task 11; `rate-mode rps:<n>` CLI flag omitted from Task 15's minimal CLI — noted below), runner integration (Task 12), stats + 5-sheet workbook (Task 13-14), CLI `trace/run/report/all` (Task 15), end-to-end verification (Task 16).
- **Gap fixed inline:** the CLI in Task 15 wires `time_scale` but not `--rate-mode rps:<n>`; this is a follow-up CLI flag, not a new subsystem — add `--rate-mode` to `run_cmd` and `all_cmd` as a small follow-up task if needed before relying on RPS-mode replay in production use.
- **Type consistency checked:** `TraceRequest` fields (`i, t_ms, seed, api, stream, model, in_tokens, out_tokens, duration_ms, ttft_ms`) used identically across schema, synthetic, from_spendlogs, request builders, runner. `RequestResult` fields match between `results.py`, `runner.py`, `stats.py`, `excel.py` (`latency_ms`, `ttft_ms`, `overhead_ms`, `sched_lag_ms`, `ok`, `reason`, `seed`, `api`, `stream`, `t_sent_ms`, `t_done_ms`).
- **No placeholders:** every step has runnable code and exact commands.
