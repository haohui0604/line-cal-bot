"""夜食と間食は同じ区分（snack）で扱う、という仕様の固定."""

from app.handlers.text_handler import _slot_jp, _slot_to_english


def test_you_shoku_is_snack():
    assert _slot_to_english("夜食") == "snack"


def test_kan_shoku_is_snack():
    assert _slot_to_english("間食") == "snack"


def test_yu_shoku_is_dinner():
    assert _slot_to_english("夕食") == "dinner"


def test_asagohan_is_breakfast():
    assert _slot_to_english("朝食") == "breakfast"


def test_slot_jp_snack():
    assert _slot_jp("snack") == "間食"


def test_day_view_has_snack_label():
    import app.services.day_view as dv
    names = [n for n in dir(dv) if "SLOT" in n.upper()]
    assert names
    assert getattr(dv, names[0]).get("snack") == "間食"
