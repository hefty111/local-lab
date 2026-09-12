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
