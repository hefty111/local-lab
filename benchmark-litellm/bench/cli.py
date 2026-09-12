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
