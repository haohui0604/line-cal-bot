import time

from app.handlers import text_handler as th
from app.services import dates as jst


def _seed(uid, **kw):
    th._pending.clear()
    p = {"foods": [{"name": "カレー", "kcal": 600}], "meal_slot": "snack",
         "date": "2026-10-02", "awaiting_slot": True, "created_at": time.time()}
    p.update(kw)
    th._pending[uid] = p
    return p


def test_slot_choice_today():
    _seed("u1")
    th.handle_slot_choice("u1", "0:lunch")
    assert th._pending["u1"]["meal_slot"] == "lunch"
    assert th._pending["u1"]["date"] == jst.today_jst()
    assert th._pending["u1"]["awaiting_slot"] is False


def test_slot_choice_yesterday_night():
    _seed("u2")
    th.handle_slot_choice("u2", "1:night")
    assert th._pending["u2"]["meal_slot"] == "night"
    assert th._pending["u2"]["date"] == jst.yesterday_jst()


def test_slot_choice_other_sets_flag():
    _seed("u3")
    th.handle_slot_choice("u3", "other")
    assert th._pending["u3"]["awaiting_other"] is True


def test_other_free_text_resolves():
    _seed("u4", awaiting_other=True)
    th._resolve_other_slot("u4", "おとといのひる")
    p = th._pending["u4"]
    assert p["meal_slot"] == "lunch"
    assert p["date"] == (jst.today_jst_date().fromordinal(
        jst.today_jst_date().toordinal() - 2)).isoformat()
    assert p["awaiting_other"] is False


def test_other_free_text_unparsable_keeps_pending():
    _seed("u5", awaiting_other=True)
    r = th._resolve_other_slot("u5", "でたらめ")
    assert r is not None and "u5" in th._pending
    assert th._pending["u5"]["awaiting_other"] is True


def test_finalize_timeout_registers_snack(monkeypatch):
    saved = {}
    monkeypatch.setattr(th, "_save_foods",
                        lambda uid, foods, slot, d=None: saved.update(
                            uid=uid, slot=slot, d=d, n=len(foods)))
    _seed("u6", created_at=time.time() - 1000)
    n = th.finalize_pending("u6")
    assert n and "間食" in n
    assert saved["slot"] == "snack" and saved["d"] == "2026-10-02"
    assert "u6" not in th._pending


def test_finalize_not_yet_when_within_timeout():
    _seed("u7")
    assert th.finalize_pending("u7") is None
    assert "u7" in th._pending


def test_finalize_on_other_message_when_slot_chosen(monkeypatch):
    saved = {}
    monkeypatch.setattr(th, "_save_foods",
                        lambda uid, foods, slot, d=None: saved.update(slot=slot))
    _seed("u8", awaiting_slot=False, meal_slot="dinner")
    n = th.finalize_pending("u8", incoming_text="おはよう")
    assert n and saved["slot"] == "dinner"


def test_finalize_skips_yes(monkeypatch):
    monkeypatch.setattr(th, "_save_foods", lambda *a, **k: None)
    _seed("u9", awaiting_slot=False)
    assert th.finalize_pending("u9", incoming_text="はい") is None
    assert "u9" in th._pending


def test_cancel_clears_pending_without_saving(monkeypatch):
    calls = []
    monkeypatch.setattr(th, "_save_foods", lambda *a, **k: calls.append(a))
    _seed("c1")
    msg = th.handle_slot_choice("c1", "cancel")
    assert "キャンセル" in msg.text
    assert "c1" not in th._pending
    assert calls == []


def test_cancel_works_during_other_wait():
    _seed("c2", awaiting_other=True)
    th.handle_slot_choice("c2", "cancel")
    assert "c2" not in th._pending


def test_cancel_duplicate_is_safe():
    _seed("c3")
    th.handle_slot_choice("c3", "cancel")
    msg = th.handle_slot_choice("c3", "cancel")
    assert "キャンセル" in msg.text
    assert "c3" not in th._pending


def test_finalize_after_cancel_does_nothing(monkeypatch):
    monkeypatch.setattr(th, "_save_foods", lambda *a, **k: None)
    _seed("c4")
    th.handle_slot_choice("c4", "cancel")
    assert th.finalize_pending("c4", incoming_text="おはよう") is None


def test_with_notice_returns_separate_messages():
    from linebot.models import TextSendMessage
    from app import webhook
    out = webhook._with_notice(TextSendMessage(text="本文"), "⏱ 確定しました")
    assert isinstance(out, list) and len(out) == 2
    assert out[0].text.startswith("⏱")
    assert out[1].text == "本文"


def test_with_notice_caps_at_five():
    from linebot.models import TextSendMessage
    from app import webhook
    out = webhook._with_notice([TextSendMessage(text=str(i)) for i in range(6)], "n")
    assert len(out) == 5 and out[0].text == "n"
