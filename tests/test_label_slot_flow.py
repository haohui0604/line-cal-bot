"""成分表（mode=label）も写真と同じ「いつの食事か」選択フローを通ること."""
import time

from app.handlers import image_handler as ih
from app.handlers import text_handler as th

LABEL_JSON = ('{"mode":"label","name":"サラダチキン","brand":"テスト社",'
              '"kcal":110,"protein_g":24,"fat_g":1.5,"carb_g":0.5,"salt_g":1.0}')


class FakeContent:
    content_type = "image/jpeg"

    def iter_content(self):
        yield b"dummy"


class FakeApi:
    def get_message_content(self, mid):
        return FakeContent()


def test_label_uses_slot_quick_reply_and_saves_with_slot(monkeypatch):
    th._pending.clear()
    monkeypatch.setattr(ih, "extract_label", lambda *a, **k: LABEL_JSON)
    saved = []
    monkeypatch.setattr(ih, "save_entry", lambda **k: saved.append(k))

    out = ih.handle_image("Ulabel", "m1", FakeApi())

    assert saved == []                       # 即時保存しない
    assert out.quick_reply is not None       # 「いつの食事か」の選択肢が出る
    p = th._pending["Ulabel"]
    assert p["awaiting_slot"] is True
    assert p["source_type"] == "ocr_label"
    assert p["foods"][0]["name"] == "サラダチキン"
    assert p["foods"][0]["kcal"] == 110.0

    r = th.handle_slot_choice("Ulabel", "0:breakfast")
    assert "朝" in r.text and "サラダチキン" in r.text

    saved2 = []
    monkeypatch.setattr(th, "save_entry", lambda **k: saved2.append(k))
    th.handle_text("Ulabel", "はい")
    assert saved2 and saved2[0]["meal_slot"] == "breakfast"
    assert saved2[0]["source_type"] == "ocr_label"
    assert saved2[0]["confidence"] == "confirmed"
    assert saved2[0]["food_name"] == "サラダチキン"


def test_label_timeout_falls_back_to_snack(monkeypatch):
    th._pending.clear()
    monkeypatch.setattr(ih, "extract_label", lambda *a, **k: LABEL_JSON)
    monkeypatch.setattr(ih, "save_entry", lambda **k: None)
    ih.handle_image("Ulabel2", "m1", FakeApi())
    th._pending["Ulabel2"]["created_at"] = time.time() - 1000  # 15分超

    saved = []
    monkeypatch.setattr(th, "save_entry", lambda **k: saved.append(k))
    notice = th.finalize_pending("Ulabel2")
    assert saved and saved[0]["meal_slot"] == "snack"
    assert saved[0]["source_type"] == "ocr_label"
    assert "間食" in notice


def test_label_cancel_does_not_save(monkeypatch):
    th._pending.clear()
    monkeypatch.setattr(ih, "extract_label", lambda *a, **k: LABEL_JSON)
    monkeypatch.setattr(ih, "save_entry", lambda **k: None)
    ih.handle_image("Ulabel3", "m1", FakeApi())
    saved = []
    monkeypatch.setattr(th, "save_entry", lambda **k: saved.append(k))
    out = th.handle_slot_choice("Ulabel3", "cancel")
    assert saved == []
    assert "キャンセル" in out.text
    assert "Ulabel3" not in th._pending
