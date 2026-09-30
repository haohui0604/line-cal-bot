"""オーナーによる会員解除: 権限・見え方・再入会の回帰テスト."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "rm.db")
os.environ["TURSO_DATABASE_URL"] = ""
os.environ["GEMINI_API_KEY"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app.services.db import init_db
from app.services import gym_db
from app import auth
from app.main import app

DAY = "2026-09-29"


@pytest.fixture(scope="module", autouse=True)
def _seed():
    init_db()
    for u in ("U_own", "U_t1", "U_t2", "U_mem"):
        gym_db.upsert_user(u)
    g = gym_db.create_gym(name="解除テストジム", owner_user_id="U_own")
    globals()["GYM"] = g
    code = gym_db.create_trainer_invite(g["id"], "U_own")
    assert gym_db.use_trainer_invite(code, "U_t1")["result"] == "ok"
    res = gym_db.request_join(user_id="U_mem", gym_id=g["id"])
    assert gym_db.approve_request(res["membership_id"], trainer_id="U_t1")


def _mid():
    """会員が解除済みでも再入会させてから membership_id を返す（順序非依存）."""
    ms = gym_db.list_members_for_gym(GYM["id"])
    if not ms:
        res = gym_db.request_join(user_id="U_mem", gym_id=GYM["id"])
        if res["result"] == "requested":
            gym_db.approve_request(res["membership_id"], trainer_id="U_t1")
        ms = gym_db.list_members_for_gym(GYM["id"])
    return ms[0]["membership_id"]


def test_member_visible_to_owner_and_assigned_trainer():
    assert [m["user_id"] for m in gym_db.list_members_for_gym(GYM["id"])] == ["U_mem"]
    assert gym_db.can_staff_view_member("U_own", "U_mem") is True
    assert gym_db.can_staff_view_member("U_t1", "U_mem") is True   # 担当
    assert gym_db.can_staff_view_member("U_t2", "U_mem") is False  # 非担当


def test_non_owner_cannot_remove():
    """別ジムのオーナーや非担当トレーナーは解除できない."""
    g2 = gym_db.create_gym(name="別ジム2", owner_user_id="U_t2")
    assert gym_db.remove_member(_mid(), by_user_id="U_t2")["result"] == "forbidden"
    assert gym_db.remove_member(_mid(), by_user_id="U_t1")["result"] == "forbidden"
    assert gym_db.remove_member(99999, by_user_id="U_own")["result"] == "not_found"
    assert len(gym_db.list_members_for_gym(GYM["id"])) == 1  # まだ居る
    assert g2["id"] != GYM["id"]


def test_owner_remove_makes_member_invisible():
    res = gym_db.remove_member(_mid(), by_user_id="U_own")
    assert res["result"] == "ok" and res["member_id"] == "U_mem"
    assert gym_db.list_members_for_gym(GYM["id"]) == []
    assert gym_db.can_staff_view_member("U_own", "U_mem") is False
    assert gym_db.can_staff_view_member("U_t1", "U_mem") is False
    # 会員側の所属表示も active を優先するため旧ジムが出ない
    ms = gym_db.get_membership_for_user("U_mem")
    assert ms is None or ms["status"] != "active"


def test_removed_member_can_rejoin():
    res = gym_db.request_join(user_id="U_mem", gym_id=GYM["id"])
    assert res["result"] == "requested"
    assert gym_db.approve_request(res["membership_id"], trainer_id="U_t1")
    assert gym_db.can_staff_view_member("U_own", "U_mem") is True


def test_records_survive_removal():
    """解除しても食事記録そのものは消えない（紐づけだけ切れる）."""
    from app.services import db, day_view
    db.save_entry(user_id="U_mem", date=DAY, meal_slot="lunch",
                  food_name="そば", kcal=400, protein_g=12, fat_g=6,
                  carb_g=70, source_type="user_report", confidence="confirmed")
    gym_db.remove_member(_mid(), by_user_id="U_own")
    assert day_view.build_day_data("U_mem", DAY)["intake_kcal"] == 400


def test_api_requires_login_and_owner(monkeypatch):
    client = TestClient(app, raise_server_exceptions=False)
    # 未ログイン → 401
    r = client.post("/api/admin/gym/member/remove",
                    json={"membership_id": _mid()})
    assert r.status_code == 401
    # 会員としてログインしても 403（gym_admin ではない）
    monkeypatch.setattr(auth, "current_user_id", lambda req: "U_mem")
    r = client.post("/api/admin/gym/member/remove",
                    json={"membership_id": _mid()})
    assert r.status_code == 403
    assert gym_db.can_staff_view_member("U_own", "U_mem") is True


def test_admin_page_lists_members(monkeypatch):
    _mid()   # 会員が居る状態にする
    monkeypatch.setattr(auth, "current_user_id", lambda req: "U_own")
    client = TestClient(app)
    r = client.get("/admin/gym")
    assert r.status_code == 200
    html = r.text
    assert "会員（1名）" in html and "解除" in html
    assert 'data-mid="' in html


def test_join_message_states_visibility():
    from app import webhook
    import inspect
    src = inspect.getsource(webhook._handle_join_code)
    assert "オーナーと担当トレーナーが" in src
    assert "閲覧できます" in src
