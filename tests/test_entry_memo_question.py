"""記録ごとの自分メモ(memo)とトレーナーへの質問(question)の回帰テスト."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "memo.db")
os.environ["TURSO_DATABASE_URL"] = ""
os.environ["GEMINI_API_KEY"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app.services.db import init_db
from app.services import db, day_view, gym_db
from app import auth
from app.main import app

DAY = "2026-09-28"


@pytest.fixture(scope="module", autouse=True)
def _seed():
    init_db()
    gym_db.upsert_user("Umemo")
    gym_db.upsert_user("Unote_other")
    db.save_entry(user_id="Umemo", date=DAY, meal_slot="lunch",
                  food_name="牛丼", kcal=700, protein_g=20, fat_g=25,
                  carb_g=90, source_type="user_report", confidence="confirmed")


@pytest.fixture()
def client():
    return TestClient(app)


def _as(monkeypatch, uid):
    monkeypatch.setattr(auth, "verify_id_token",
                        lambda t: {"sub": uid, "name": uid})
    monkeypatch.setattr(gym_db, "upsert_user", lambda *a, **k: None)


def _eid():
    return day_view.build_day_data("Umemo", DAY)["entries"][0]["id"]


def _memo(client, uid, body, eid=None):
    return client.post("/api/day/entries", json={
        "action": "memo", "id_token": uid,
        "entry_id": eid if eid is not None else _eid(), "body": body})


def _question(client, uid, body, eid=None):
    return client.post("/api/day/entries", json={
        "action": "question", "id_token": uid,
        "entry_id": eid if eid is not None else _eid(), "body": body})


def test_memo_save_show_clear(client, monkeypatch):
    _as(monkeypatch, "Umemo")
    r = _memo(client, "Umemo", "塩分多めだった")
    assert r.status_code == 200 and r.json()["ok"]
    e = day_view.build_day_data("Umemo", DAY)["entries"][0]
    assert e["note"] == "塩分多めだった"
    # 上書き
    r = _memo(client, "Umemo", "夜も我慢できた")
    assert r.status_code == 200
    assert day_view.build_day_data("Umemo", DAY)["entries"][0]["note"] == "夜も我慢できた"
    # 空にすると削除
    r = _memo(client, "Umemo", "")
    assert r.status_code == 200
    assert day_view.build_day_data("Umemo", DAY)["entries"][0]["note"] == ""


def test_memo_other_user_rejected(client, monkeypatch):
    _as(monkeypatch, "Unote_other")
    r = _memo(client, "Unote_other", "のぞき見")
    assert r.status_code == 404
    e = day_view.build_day_data("Umemo", DAY)["entries"][0]
    assert not (e["note"] or "")


def test_question_saved_and_visible_in_day_comments(client, monkeypatch):
    _as(monkeypatch, "Umemo")
    monkeypatch.setattr(gym_db, "get_member_trainer", lambda u: None)
    r = _question(client, "Umemo", "この揚げ物の脂質は推定で合ってますか？")
    assert r.status_code == 200
    assert r.json()["pushed"] is False   # 担当T未設定 → 通知なし
    cms = gym_db.fetch_comments_for_date("Umemo", DAY)
    q = [c for c in cms if c["author_type"] == "member"]
    assert q  # 会員の質問コメントが保存されている
    assert "牛丼" in q[0]["body"] and "この揚げ物の脂質" in q[0]["body"]
    assert q[0]["target_date"] == DAY


def test_question_pushes_to_assigned_trainer(client, monkeypatch):
    _as(monkeypatch, "Umemo")
    pushed = {}
    monkeypatch.setattr(gym_db, "get_member_trainer", lambda u: "UtrainerX")
    monkeypatch.setattr(gym_db, "get_user",
                        lambda u: {"display_name": "会員A"})
    import app.web.day as day_mod
    monkeypatch.setattr(day_mod, "_push_to_member",
                        lambda uid, text: pushed.update({"uid": uid, "text": text}))
    r = _question(client, "Umemo", "PFCの割合の目安は？")
    assert r.status_code == 200 and r.json()["pushed"] is True
    assert pushed["uid"] == "UtrainerX"
    assert "会員A" in pushed["text"] and "PFCの割合" in pushed["text"]


def test_question_other_user_entry_rejected(client, monkeypatch):
    _as(monkeypatch, "Unote_other")
    r = _question(client, "Unote_other", "他人の記録に質問")
    assert r.status_code == 404
    cms = gym_db.fetch_comments_for_date("Umemo", DAY)
    assert all("他人の記録に質問" not in c["body"] for c in cms)


def test_template_has_note_and_question_buttons():
    html = Path("app/templates/day_detail.html").read_text(encoding="utf-8")
    assert "editNote" in html and "askQ" in html
    assert "data-note" in html and '"question"' in html and '"memo"' in html
