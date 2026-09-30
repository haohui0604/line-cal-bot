"""Phase 3.5f: 目的設定のボタン化確認 + トレーナー画面への導線.

背景:
- 要件「選択肢はボタンで回答できるように」は既に goals.py で実装済み。
  ここでは回帰防止のため、主要な選択肢が Quick Reply になっていることを検証する。
- トレーナー画面への導線は /api/me/role が is_staff を返し、
  /me 画面が権限に応じてリンクを出す形で実装した。
"""
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import goals, gym_db          # noqa: E402
from app.services.db import init_db             # noqa: E402
from app.web import member as member_mod        # noqa: E402
from app import auth                            # noqa: E402


def _labels(qr_obj):
    """QuickReply の items から label の一覧を取り出す."""
    items = getattr(qr_obj, "items", None) or []
    out = []
    for it in items:
        act = getattr(it, "action", None)
        out.append(getattr(act, "label", None))
    return out


def test_wizard_mode_choice_is_buttons():
    uid = "Uqr_test_1"
    init_db()
    goals._pending.pop(uid, None)
    msg = goals.start_wizard(uid)
    assert msg.quick_reply is not None, "目的選択がボタンになっていません"
    labels = _labels(msg.quick_reply)
    assert any("減量" in (x or "") for x in labels)
    assert any("減塩" in (x or "") for x in labels)
    assert any("筋肉" in (x or "") for x in labels)
    goals._pending.pop(uid, None)


def test_wizard_period_choice_is_buttons():
    uid = "Uqr_test_2"
    init_db()
    goals._pending[uid] = {"step": "w_target", "current": 75.0}
    msg = goals.handle_step(uid, "65")   # 目標体重入力 → 期間選択へ
    assert msg is not None and msg.quick_reply is not None
    labels = _labels(msg.quick_reply)
    assert any("1ヶ月" in (x or "") for x in labels)
    goals._pending.pop(uid, None)


def test_wizard_sex_choice_is_buttons(monkeypatch):
    """活動量データが無い場合の性別選択もボタン."""
    uid = "Uqr_test_3"
    init_db()
    monkeypatch.setattr(goals, "_activity_avg_7d", lambda u: None)
    goals._pending[uid] = {"step": "days", "current": 75.0, "target": 65.0}
    msg = goals.handle_step(uid, "期間 60")
    assert msg is not None and msg.quick_reply is not None
    labels = _labels(msg.quick_reply)
    assert "男性" in labels and "女性" in labels
    goals._pending.pop(uid, None)


# --- トレーナー導線: /api/me/role ---
STAFF_U = "Ustaff_role_test"
PLAIN_U = "Uplain_role_test"


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(member_mod.router)
    return TestClient(app)


def _mk_staff(monkeypatch):
    init_db()
    gym_db.upsert_user(STAFF_U, "スタッフ", None)
    gid = gym_db.create_gym(name="テストジム", owner_user_id=STAFF_U)
    monkeypatch.setattr(auth, "verify_id_token",
                        lambda tok: {"sub": STAFF_U, "name": "s", "picture": None})
    return gid


def test_role_api_returns_staff_true(client, monkeypatch):
    _mk_staff(monkeypatch)
    r = client.post("/api/me/role", json={"id_token": "dummy"})
    assert r.status_code == 200
    assert r.json()["is_staff"] is True


def test_role_api_returns_staff_false_for_plain(client, monkeypatch):
    init_db()
    gym_db.upsert_user(PLAIN_U, "一般会員", None)
    monkeypatch.setattr(auth, "verify_id_token",
                        lambda tok: {"sub": PLAIN_U, "name": "p", "picture": None})
    r = client.post("/api/me/role", json={"id_token": "dummy"})
    assert r.status_code == 200
    assert r.json()["is_staff"] is False


def test_help_text_has_trainer_entry():
    html = (ROOT / "app" / "handlers" / "text_handler.py").read_text(
        encoding="utf-8")
    assert "/trainer" in html, "ヘルプにトレーナー画面のURLがありません"


def test_member_page_has_trainer_link_hook():
    html = (ROOT / "app" / "templates" / "member_home.html").read_text(
        encoding="utf-8")
    assert "trainerLink" in html and "/trainer" in html
