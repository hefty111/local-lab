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
