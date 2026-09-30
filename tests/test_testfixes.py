"""ユーザーの通しテストで見つかったNG項目の回帰テスト (phase35m).

A5: OCR消費カロリーは画像内の日付で記録される
B1: 「初期設定」は目的設定ウィザード(ボタン式)に進む
C4: 修正フォームにPFCが初期表示される
C5: kcal空欄でも追加できる(AI推定) + エラーが残らない
"""
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.handlers import image_handler, text_handler      # noqa: E402
from app.services import gym_db, goals                     # noqa: E402
from app.services.db import init_db, get_conn              # noqa: E402
from app.web import day as day_mod                         # noqa: E402
from app import auth                                       # noqa: E402

MEMBER = "Ufix_test"


# ---- A5 ----
def _fake_ocr_activity(monkeypatch, record_date):
    data = {"mode": "activity", "total_burn_kcal": 2450,
            "active_kcal": 464, "resting_kcal": 1986,
            "record_date": record_date}
    monkeypatch.setattr(image_handler, "extract_label",
                        lambda b, mime_type=None: data)

    class _C:
        content_type = "image/jpeg"
        def iter_content(self): return [b"img"]

    class _Api:
        def get_message_content(self, mid): return _C()
    return _Api()


def test_ocr_activity_uses_image_date(monkeypatch):
    init_db()
    gym_db.upsert_user(MEMBER, "t", None)
    api = _fake_ocr_activity(monkeypatch, "2026-09-29")
    out = image_handler.handle_image(MEMBER, "m1", api)
    assert "2026-09-29" in out.text
    with get_conn() as c:
        row = c.execute(
            "SELECT date, total_kcal FROM activity WHERE user_id=?",
            (MEMBER,)).fetchone()
    assert row["date"] == "2026-09-29"


def test_ocr_activity_defaults_to_today_when_no_date(monkeypatch):
    init_db()
    gym_db.upsert_user(MEMBER, "t", None)
    api = _fake_ocr_activity(monkeypatch, None)
    out = image_handler.handle_image(MEMBER, "m2", api)
    from app.services.dates import today_jst
    assert today_jst() in out.text


def test_ocr_activity_ignores_future_date(monkeypatch):
    init_db()
    gym_db.upsert_user(MEMBER, "t", None)
    api = _fake_ocr_activity(monkeypatch, "2099-01-01")
    out = image_handler.handle_image(MEMBER, "m3", api)
    from app.services.dates import today_jst
    assert today_jst() in out.text  # 未来日は誤読として今日にフォールバック


def test_ocr_prompt_has_date_hint_and_record_date():
    from app.services import ocr
    assert "record_date" in ocr.OCR_PROMPT
    src = (ROOT / "app" / "services" / "ocr.py").read_text(encoding="utf-8")
    assert "今日は" in src and "today_jst()" in src


# ---- B1: 「初期設定」は目的設定ウィザードへ ----
def test_shoki_setup_routes_to_goal_wizard(monkeypatch):
    init_db()
    goals._pending.pop(MEMBER, None)
    text_handler._pending.pop(MEMBER, None)
    text_handler._setup_pending.pop(MEMBER, None)
    out = text_handler.handle_text(MEMBER, "初期設定")
    assert out is not None
    assert "目的" in out.text           # 旧「Q1. 目的を選んでください」ではない
    assert out.quick_reply is not None  # ボタン付き
    labels = [getattr(getattr(i, "action", None), "label", "")
              for i in getattr(out.quick_reply, "items", [])]
    assert any("減量" in x for x in labels), labels
    assert any("減塩" in x for x in labels), labels
    assert any("筋肉" in x for x in labels), labels
    goals._pending.pop(MEMBER, None)


def test_old_setup_still_reachable_via_setup_keyword():
    init_db()
    goals._pending.pop(MEMBER, None)
    text_handler._pending.pop(MEMBER, None)
    text_handler._setup_pending.pop(MEMBER, None)
    out = text_handler.handle_text(MEMBER, "セットアップ")
    assert out is not None and "初期設定を始めます" in out.text
    text_handler._setup_pending.pop(MEMBER, None)


# ---- C5: kcal空欄で追加（AI推定はモック） ----
@pytest.fixture()
def day_client():
    app = FastAPI()
    app.include_router(day_mod.router)
    init_db()
    gym_db.upsert_user(MEMBER, "t", None)
    return TestClient(app)


def test_add_without_kcal_estimates(day_client, monkeypatch):
    monkeypatch.setattr(auth, "verify_id_token",
                        lambda tok: {"sub": MEMBER, "name": "t", "picture": None})

    import app.services.day_view as dv
    def fake_estimate(item):
        item.update({"kcal": 450.0, "protein_g": 20.0,
                     "fat_g": 10.0, "carb_g": 50.0, "salt_g": 1.0})
    monkeypatch.setattr("app.services.llm.estimate_food_single", fake_estimate)

    r = day_client.post("/api/day/entries", json={
        "action": "add", "id_token": "x", "date": "2026-09-29",
        "meal_slot": "lunch", "food_name": "からあげ弁当"})
    assert r.status_code == 200, r.text
    assert r.json()["estimated"] is True
    from app.services.db import fetch_entries_for_date, get_conn
    e = fetch_entries_for_date(MEMBER, "2026-09-29")
    assert e and e[0]["kcal"] == 450.0
    with get_conn() as c:
        row = c.execute(
            "SELECT confidence FROM entries WHERE user_id=? AND date=?",
            (MEMBER, "2026-09-29")).fetchone()
    assert row["confidence"] == "estimated"


def test_add_without_food_name_rejected(day_client, monkeypatch):
    monkeypatch.setattr(auth, "verify_id_token",
                        lambda tok: {"sub": MEMBER, "name": "t", "picture": None})
    r = day_client.post("/api/day/entries", json={
        "action": "add", "id_token": "x", "date": "2026-09-29",
        "meal_slot": "lunch", "food_name": ""})
    assert r.status_code == 400
    assert "食品名は必須" in r.json()["detail"]


# ---- C4/C5: テンプレートの構造確認 ----
def test_template_edit_prefills_pfc_and_kcal_optional():
    html = (ROOT / "app" / "templates" / "day_detail.html").read_text(
        encoding="utf-8")
    assert "dataset.p" in html and "dataset.f" in html and "dataset.c" in html
    assert 'data-p="' in html          # 行にPFCを保持
    assert "kcal（空欄でAI推定）" in html
    assert "食品名とkcalは必須" not in html
    assert 'document.getElementById("err").style.display="none"' in html
