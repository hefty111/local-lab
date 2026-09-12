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
