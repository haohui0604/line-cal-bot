"""マイページ履歴修正: 区分(カテゴリ)と日付の変更 + 権限/バリデーション."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "edit.db")
os.environ["TURSO_DATABASE_URL"] = ""
os.environ["GEMINI_API_KEY"] = ""   # AI推定無効化（決定的にする）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app.services.db import init_db
from app.services import db, day_view, gym_db
from app import auth
from app.main import app

OLD_DAY = "2026-09-20"
NEW_DAY = "2026-09-21"


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_db()
    gym_db.upsert_user("Uedit")
    gym_db.upsert_user("Uother")


@pytest.fixture(autouse=True)
def _fresh_record():
    """各テストを独立させる: 旧日/新日の行を消してから旧日に1件だけ置く."""
    for d in (OLD_DAY, NEW_DAY):
        db.delete_entries_by_date("Uedit", d)
    db.save_entry(user_id="Uedit", date=OLD_DAY, meal_slot="breakfast",
                  food_name="パスタ", kcal=600, protein_g=20, fat_g=20,
                  carb_g=80, source_type="user_report", confidence="confirmed")
    yield


@pytest.fixture()
def client():
    return TestClient(app)


def _as(monkeypatch, uid):
    monkeypatch.setattr(auth, "verify_id_token",
                        lambda t: {"sub": uid, "name": uid})
    monkeypatch.setattr(gym_db, "upsert_user", lambda *a, **k: None)


def _eid():
    return day_view.build_day_data("Uedit", OLD_DAY)["entries"][0]["id"]


def _edit(client, uid, **over):
    body = {"action": "edit", "id_token": uid, "entry_id": _eid(),
            "meal_slot": "dinner", "food_name": "パスタ", "kcal": 600,
            "protein_g": 20, "fat_g": 20, "carb_g": 80, "date": NEW_DAY}
    body.update(over)
    return client.post("/api/day/entries", json=body)


def test_edit_moves_record_between_days(client, monkeypatch):
    _as(monkeypatch, "Uedit")
    before_old = day_view.build_day_data("Uedit", OLD_DAY)
    assert before_old["intake_kcal"] == 600 and len(before_old["entries"]) == 1
    assert day_view.build_day_data("Uedit", NEW_DAY)["intake_kcal"] == 0

    r = _edit(client, "Uedit")
    assert r.status_code == 200, r.text

    after_old = day_view.build_day_data("Uedit", OLD_DAY)
    after_new = day_view.build_day_data("Uedit", NEW_DAY)
    assert after_old["intake_kcal"] == 0 and len(after_old["entries"]) == 0
    assert "dinner" not in after_old["slots"]
    assert after_new["intake_kcal"] == 600
    assert len(after_new["entries"]) == 1
    assert "dinner" in after_new["slots"]
    assert [f["food_name"] for f in after_new["slots"]["dinner"]["foods"]] == ["パスタ"]
    assert db.fetch_day_summary("Uedit", OLD_DAY)["intake_kcal"] == 0
    assert db.fetch_day_summary("Uedit", NEW_DAY)["intake_kcal"] == 600
    assert db.fetch_day_summary("Uedit", NEW_DAY)["protein_g"] == 20


def test_edit_keeps_pfc_and_moves_back(client, monkeypatch):
    _as(monkeypatch, "Uedit")
    e = _edit(client, "Uedit", date=OLD_DAY, meal_slot="lunch")
    assert e.status_code == 200, e.text
    d = day_view.build_day_data("Uedit", OLD_DAY)
    assert d["intake_kcal"] == 600
    assert d["protein_g"] == 20 and d["fat_g"] == 20 and d["carb_g"] == 80
    assert "lunch" in d["slots"] and "dinner" not in d["slots"]
    assert day_view.build_day_data("Uedit", NEW_DAY)["intake_kcal"] == 0


def test_category_only_change_keeps_day(client, monkeypatch):
    _as(monkeypatch, "Uedit")
    r = _edit(client, "Uedit", meal_slot="snack", date=None)
    assert r.status_code == 200, r.text
    d = day_view.build_day_data("Uedit", OLD_DAY)
    assert d["intake_kcal"] == 600 and "snack" in d["slots"]
    assert day_view.build_day_data("Uedit", NEW_DAY)["intake_kcal"] == 0


def test_other_users_record_id_is_rejected(client, monkeypatch):
    _as(monkeypatch, "Uother")
    r = _edit(client, "Uother")
    assert r.status_code == 404, r.text
    d = day_view.build_day_data("Uedit", OLD_DAY)
    assert d["intake_kcal"] == 600 and d["entries"][0]["food_name"] == "パスタ"
    assert day_view.build_day_data("Uedit", NEW_DAY)["intake_kcal"] == 0
    assert day_view.update_entry_full(
        "Uother", _eid(), meal_slot="dinner", food_name="盗み", kcal=1.0,
        date=NEW_DAY) is False


def test_future_date_rejected(client, monkeypatch):
    _as(monkeypatch, "Uedit")
    r = _edit(client, "Uedit", date="2999-01-01")
    assert r.status_code == 400
    assert "未来" in r.json()["detail"]
    assert day_view.build_day_data("Uedit", OLD_DAY)["intake_kcal"] == 600


def test_invalid_date_format_rejected(client, monkeypatch):
    _as(monkeypatch, "Uedit")
    r = _edit(client, "Uedit", date="2026/09/21")
    assert r.status_code == 400
    assert day_view.build_day_data("Uedit", OLD_DAY)["intake_kcal"] == 600


@pytest.mark.parametrize("slot", ["brunch", "朝食", "", "unknown"])
def test_invalid_category_rejected(client, monkeypatch, slot):
    _as(monkeypatch, "Uedit")
    r = _edit(client, "Uedit", meal_slot=slot)
    assert r.status_code in (400, 422), r.text
    assert day_view.build_day_data("Uedit", OLD_DAY)["intake_kcal"] == 600


def test_duplicate_target_rejected(client, monkeypatch):
    """移動先に同名・同区分の記録がある場合は 400（UNIQUE制約を握り潰さない）."""
    _as(monkeypatch, "Uedit")
    db.save_entry(user_id="Uedit", date=NEW_DAY, meal_slot="dinner",
                  food_name="パスタ", kcal=500, protein_g=10, fat_g=10,
                  carb_g=60, source_type="user_report", confidence="confirmed")
    r = _edit(client, "Uedit", date=NEW_DAY, meal_slot="dinner")
    assert r.status_code == 400, r.text
    assert "すでにあります" in r.json()["detail"]
    assert day_view.build_day_data("Uedit", OLD_DAY)["intake_kcal"] == 600


def test_template_has_category_and_date_inputs():
    html = Path("app/templates/day_detail.html").read_text(encoding="utf-8")
    assert 'id="e_slot"' in html and 'id="e_date"' in html
    assert 'type="date"' in html
    assert "meal_slot: origSlot" not in html
