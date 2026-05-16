from tracker.interactive_search import parse_keyword_phrases


def test_split_comma():
    assert parse_keyword_phrases("a, b") == ["a", "b"]


def test_split_pipe():
    assert parse_keyword_phrases("camera | công nghệ thông tin") == [
        "camera",
        "công nghệ thông tin",
    ]


def test_single_phrase():
    assert parse_keyword_phrases("  camera  ") == ["camera"]
