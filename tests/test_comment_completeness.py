"""AIコメントが途中で切れていないかの判定ロジックのテスト."""

from app.services.coach import _is_complete, _trim_to_sentence


def test_complete_when_ends_with_kuten():
    assert _is_complete("今日はいい感じです。") is True
    assert _is_complete("すごい！") is True
    assert _is_complete("その調子」") is True


def test_incomplete_when_cut_off():
    assert _is_complete("今日はいい感じで") is False
    assert _is_complete("") is False
    assert _is_complete("   ") is False


def test_trim_keeps_last_sentence():
    assert _trim_to_sentence("Aです。Bです。Cで") == "Aです。Bです。"


def test_trim_returns_empty_when_no_kuten():
    assert _trim_to_sentence("Cで") == ""


def test_trim_handles_question_and_exclamation():
    assert _trim_to_sentence("いいね！でもここから") == "いいね！"
    assert _trim_to_sentence("どう？まだ続") == "どう？"
