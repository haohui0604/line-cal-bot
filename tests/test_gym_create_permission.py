"""ジム作成権限: 管理者 or トレーナー登録済みのユーザーが作成できること."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "gymperm.db")
os.environ["TURSO_DATABASE_URL"] = ""
os.environ["GEMINI_API_KEY"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.services.db import init_db
from app.services import gym_db
from app import webhook
from app.config import settings


class _FakeApi:
    """get_profile は失敗させて続行させる（実運用と同じ経路）."""

    def get_profile(self, uid):
        raise RuntimeError("no profile in test")


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_db()
    gym_db.upsert_user("U_plain")          # 何の登録もない一般ユーザー
    gym_db.upsert_user("U_owner")          # 既存ジムのオーナー
    gym_db.upsert_user("U_trainer")        # 既存ジムのトレーナー
    g = gym_db.create_gym(name="既存ジム", owner_user_id="U_owner")
    code = gym_db.create_trainer_invite(g["id"], "U_owner")
    assert gym_db.use_trainer_invite(code, "U_trainer")["result"] == "ok"


def test_plain_user_cannot_create_gym(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", "")
    assert webhook._can_create_gym("U_plain") is False


def test_admin_user_can_create_gym(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", "U_plain, U_other")
    assert webhook._can_create_gym("U_plain") is True


def test_registered_trainer_can_create_gym(monkeypatch):
    """トレーナー登録済みなら、管理者リストになくても作成できる（今回の要望）."""
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", "")
    assert webhook._can_create_gym("U_trainer") is True


def test_existing_gym_owner_can_create_gym(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", "")
    assert webhook._can_create_gym("U_owner") is True


def test_flag_off_restores_admin_only(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", "")
    monkeypatch.setattr(settings, "ALLOW_STAFF_GYM_CREATE", False)
    assert webhook._can_create_gym("U_trainer") is False


def test_trainer_creates_gym_and_becomes_owner(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", "")
    out = webhook._handle_create_gym("U_trainer", "マイジム", _FakeApi())
    text = out.text
    assert "マイジム" in text and "GYM-" in text
    assert "/admin/gym" in text                      # トレーナー招待の導線
    assert "オーナー権限" in text
    # 作成者自身が gym_admin(active) になっている
    admins = [m for m in gym_db.get_staff_memberships("U_trainer")
              if m["role"] == "gym_admin"]
    assert admins and admins[0]["gym_name"] == "マイジム"
    # 既存のトレーナー所属も残っている（乗っ取りではない）
    assert any(m["role"] == "trainer" for m in gym_db.get_staff_memberships("U_trainer"))
    # 新ジムの入会コードが発行されている
    code = text.split("入会コード】\n")[1].split("\n")[0]
    assert gym_db.find_gym_by_code(code)["name"] == "マイジム"


def test_plain_user_gets_guidance_not_creation(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_USER_IDS", "")
    before = len(gym_db.get_staff_memberships("U_plain"))
    out = webhook._handle_create_gym("U_plain", "できちゃったジム", _FakeApi())
    assert "作成は、管理者またはすでにトレーナー登録" in out.text
    assert "TR-XXXX" in out.text                     # 解決策を案内している
    assert gym_db.get_user("U_plain") is not None    # 何も作られていない
    assert len(gym_db.get_staff_memberships("U_plain")) == before
