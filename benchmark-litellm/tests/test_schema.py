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
