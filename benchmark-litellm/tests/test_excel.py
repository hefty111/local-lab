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


def test_build_workbook_handles_colliding_truncated_sheet_names(tmp_path):
    out_path = tmp_path / "report.xlsx"
    label_a = "a" * 30 + "_baseline"
    label_b = "a" * 30 + "_treatment"
    runs = {label_a: _records(label_a, n=1), label_b: _records(label_b, n=1)}

    build_workbook(runs, out_path)

    wb = load_workbook(out_path)
    raw_sheet_names = [name for name in wb.sheetnames if name.startswith("Raw-")]
    assert len(raw_sheet_names) == 2
    assert len(set(raw_sheet_names)) == 2
    for name in raw_sheet_names:
        assert len(name) <= 31

    ws_a, ws_b = wb[raw_sheet_names[0]], wb[raw_sheet_names[1]]

    def seeds_in_sheet(ws):
        header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        seed_idx = header.index("seed")
        return [row[seed_idx].value for row in ws.iter_rows(min_row=2)]

    all_seeds_a = seeds_in_sheet(ws_a)
    all_seeds_b = seeds_in_sheet(ws_b)
    assert all(s.startswith(label_a) for s in all_seeds_a) or all(s.startswith(label_b) for s in all_seeds_a)
    combined = set(all_seeds_a) | set(all_seeds_b)
    assert any(s.startswith(label_a) for s in combined)
    assert any(s.startswith(label_b) for s in combined)
    # ensure no sheet mixes both labels' seeds
    assert not (any(s.startswith(label_a) for s in all_seeds_a) and any(s.startswith(label_b) for s in all_seeds_a))
    assert not (any(s.startswith(label_a) for s in all_seeds_b) and any(s.startswith(label_b) for s in all_seeds_b))
