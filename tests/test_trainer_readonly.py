"""トレーナーは会員の記録を追加・修正・削除できないことの回帰テスト (Phase 3.5e).

- トレーナー (member_id 指定) は add / edit / delete が 403
- 会員本人 (id_token) は add が成功する
- テンプレートは CAN_EDIT フラグでボタン/追加フォームを出し分ける
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.web import day as day_mod
from app import auth
from app.services import gym_db
from app.services.db import init_db, save_entry, fetch_entries_for_date

MEMBER = "Umember_readonly_test"
STAFF = "Ustaff_readonly_test"
DAY = "2026-09-29"


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(day_mod.router)
    return TestClient(app)


@pytest.fixture()
def member_with_entry():
    init_db()
    gym_db.upsert_user(MEMBER, "テスト会員", None)
    save_entry(user_id=MEMBER, date=DAY, meal_slot="lunch",
               food_name="テスト飯", kcal=500, protein_g=10.0,
               fat_g=5.0, carb_g=60.0, salt_g=1.0,
               source_type="user_report", confidence="confirmed")
    entry_id = fetch_entries_for_date(MEMBER, DAY)[0]["id"]
    return entry_id


def _as_trainer(monkeypatch):
    monkeypatch.setattr(auth, "current_user_id", lambda req: STAFF)
    monkeypatch.setattr(gym_db, "can_staff_view_member", lambda s, m: True)


def _as_member(monkeypatch):
    monkeypatch.setattr(auth, "verify_id_token",
                        lambda tok: {"sub": MEMBER, "name": "テスト会員",
                                     "picture": None})


def test_trainer_cannot_add(client, member_with_entry, monkeypatch):
    _as_trainer(monkeypatch)
    r = client.post("/api/day/entries", json={
        "action": "add", "member_id": MEMBER, "date": DAY,
        "meal_slot": "dinner", "food_name": "横入り飯", "kcal": 100,
        "protein_g": 1, "fat_g": 1, "carb_g": 1})
    assert r.status_code == 403
    # DB が変わっていないこと
    assert len(fetch_entries_for_date(MEMBER, DAY)) == 1


def test_trainer_cannot_edit(client, member_with_entry, monkeypatch):
    _as_trainer(monkeypatch)
    eid = member_with_entry
    r = client.post("/api/day/entries", json={
        "action": "edit", "member_id": MEMBER, "entry_id": eid,
        "meal_slot": "lunch", "food_name": "書き換え", "kcal": 9999})
    assert r.status_code == 403
    e = fetch_entries_for_date(MEMBER, DAY)[0]
    assert e["food_name"] == "テスト飯" and e["kcal"] == 500


def test_trainer_cannot_delete(client, member_with_entry, monkeypatch):
    _as_trainer(monkeypatch)
    r = client.post("/api/day/entries", json={
        "action": "delete", "member_id": MEMBER,
        "entry_id": member_with_entry})
    assert r.status_code == 403
    assert len(fetch_entries_for_date(MEMBER, DAY)) == 1


def test_member_can_add(client, member_with_entry, monkeypatch):
    _as_member(monkeypatch)
    r = client.post("/api/day/entries", json={
        "action": "add", "id_token": "dummy", "date": DAY,
        "meal_slot": "snack", "food_name": "自分の間食", "kcal": 200,
        "protein_g": 5, "fat_g": 5, "carb_g": 20})
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True
    names = [e["food_name"] for e in fetch_entries_for_date(MEMBER, DAY)]
    assert "自分の間食" in names


def test_member_can_edit_and_delete(client, member_with_entry, monkeypatch):
    _as_member(monkeypatch)
    eid = member_with_entry
    r = client.post("/api/day/entries", json={
        "action": "edit", "id_token": "dummy", "entry_id": eid,
        "meal_slot": "lunch", "food_name": "テスト飯(修正)", "kcal": 600})
    assert r.status_code == 200, r.text
    r = client.post("/api/day/entries", json={
        "action": "delete", "id_token": "dummy", "entry_id": eid})
    assert r.status_code == 200, r.text
    # 同一セッション内の他テストの記録が残りうるので「対象idが消えたこと」で判定
    assert all(e["id"] != eid for e in fetch_entries_for_date(MEMBER, DAY))


def _capture_template_ctx(monkeypatch):
    """TemplateResponse を横取りして、テンプレートに渡されたコンテキストを返す."""
    captured = {}

    def fake(name, ctx):
        captured["name"] = name
        captured.update(ctx)
        return ctx

    monkeypatch.setattr(day_mod.templates, "TemplateResponse", fake)
    return captured


class _FakeReq:
    cookies = {}
    headers = {}


def test_trainer_day_page_is_readonly_flagged(member_with_entry, monkeypatch):
    _as_trainer(monkeypatch)
    captured = _capture_template_ctx(monkeypatch)
    day_mod.day_page(_FakeReq(), member_id=MEMBER, date="")
    assert captured["name"] == "day_detail.html"
    assert captured["can_edit"] is False   # トレーナーは記録を操作できない
    assert captured["can_comment"] is True  # コメントはできる
    assert captured["member_id"] == MEMBER


def test_member_day_page_is_editable_flagged(monkeypatch):
    init_db()
    _as_member(monkeypatch)
    captured = _capture_template_ctx(monkeypatch)
    day_mod.day_page(_FakeReq(), member_id="", date="")
    assert captured["name"] == "day_detail.html"
    assert captured["can_edit"] is True     # 会員本人は編集できる
    assert captured["can_comment"] is False
    assert captured["member_id"] is None


def test_template_hides_edit_controls_for_trainer():
    """テンプレートが can_edit=False のとき修正/削除ボタンと追加フォームを出さない."""
    import re
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(str(ROOT / "app" / "templates")))
    tpl = env.get_template("day_detail.html")
    html = tpl.render(request=None, member_id=MEMBER, can_comment=True,
                      can_edit=False, liff_id=None, initial_date="")
    assert re.search(r"CAN_EDIT\s*=\s*false", html)
    assert "閲覧とコメントのみ" in html
    assert "＋ 記録を手動追加" not in html  # 追加フォームは出ない
    html2 = tpl.render(request=None, member_id=None, can_comment=False,
                       can_edit=True, liff_id="x", initial_date="")
    assert re.search(r"CAN_EDIT\s*=\s*true", html2)
    assert "＋ 記録を手動追加" in html2
