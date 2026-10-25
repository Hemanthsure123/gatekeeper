from textkit import slugify, truncate, word_count


def test_slugify_joins_words_with_hyphens() -> None:
    assert slugify("Hello, World!") == "hello-world"


def test_slugify_strips_edge_hyphens() -> None:
    assert slugify("  --Already sluggy--  ") == "already-sluggy"


def test_word_count() -> None:
    assert word_count("one  two\nthree") == 3


def test_truncate_keeps_short_text() -> None:
    assert truncate("short", 10) == "short"


def test_truncate_result_never_exceeds_width() -> None:
    assert truncate("abcdefghij", 5) == "ab..."
    assert len(truncate("abcdefghij", 5)) == 5


def test_truncate_with_width_smaller_than_suffix() -> None:
    assert truncate("abcdefghij", 2) == ".."
