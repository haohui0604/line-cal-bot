"""トレーナー画面の「再アクセスしたらエラー」対策の回帰テスト.

検証すること:
  1. プロセス再起動で OAuth state が消えても 400 で止めない（続行する）
  2. state が有効なら使い捨てる（使い回し防止）
  3. code が無い／LINEがエラーを返した場合は、行き止まりではなく
     ログインやり直しボタン付きのHTMLを返す
  4. セッション鍵は SESSION_SECRET 未設定でも決定的（再起動でCookieが消えない）
  5. スタッフでない人のログイン後は /trainer-invite へ送る
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "login.db")
os.environ["TURSO_DATABASE_URL"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app import auth
from app.config import settings
from app.services.db import init_db
from app.web import routes


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_db()


@pytest.fixture(autouse=True)
def _clean_states():
    routes._pending_states.clear()
    yield
    routes._pending_states.clear()


def _stub_login(monkeypatch, *, uid="U_TRAINER", staff=True):
    monkeypatch.setattr(auth, "exchange_code",
                        lambda code: {"id_token": "dummy"})
    monkeypatch.setattr(auth, "verify_id_token",
                        lambda tok: {"sub": uid, "name": "テスト",
                                     "picture": None})
    monkeypatch.setattr(routes, "upsert_user", lambda *a, **k: None)
    monkeypatch.setattr(routes, "is_staff", lambda u: staff)


def test_callback_continues_when_state_lost_after_restart(monkeypatch):
    """再起動で _pending_states が空でも、ログインは成立すること."""
    _stub_login(monkeypatch)
    assert routes._pending_states == set()          # 再起動直後の状態

    resp = routes.auth_callback(request=None, code="valid-code",
                                state="lost-state")

    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/trainer"
    cookie = resp.headers.get("set-cookie", "")
    assert auth.SESSION_COOKIE in cookie and cookie.strip()
    # 発行されたCookieがそのまま読めること
    assert auth.read_session(
        auth.issue_session("U_TRAINER")) == "U_TRAINER"


def test_callback_consumes_known_state(monkeypatch):
    _stub_login(monkeypatch)
    routes._pending_states.add("known-state")
    routes.auth_callback(request=None, code="c", state="known-state")
    assert "known-state" not in routes._pending_states


def test_callback_without_code_returns_retry_page(monkeypatch):
    _stub_login(monkeypatch)
    resp = routes.auth_callback(request=None, code="", state="")
    body = resp.body.decode("utf-8")
    assert resp.status_code == 400
    assert "もう一度ログインする" in body
    assert 'href="/login"' in body


def test_callback_with_line_error_returns_page(monkeypatch):
    _stub_login(monkeypatch)
    resp = routes.auth_callback(request=None, code="", state="",
                                error="access_denied")
    assert resp.status_code == 400
    assert "LINEログインが完了しませんでした" in resp.body.decode("utf-8")


def test_non_staff_goes_to_trainer_invite(monkeypatch):
    _stub_login(monkeypatch, staff=False)
    resp = routes.auth_callback(request=None, code="c", state="")
    assert resp.headers["location"] == "/trainer-invite"


def test_login_without_config_returns_page(monkeypatch):
    monkeypatch.setattr(auth, "login_configured", lambda: False)
    resp = routes.login()
    assert resp.status_code == 503
    assert "LINEログインが未設定" in resp.body.decode("utf-8")


# ---- セッション鍵 ----

def test_session_secret_derived_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SECRET", "")
    monkeypatch.setattr(settings, "LINE_LOGIN_CHANNEL_SECRET", "ch-secret")
    assert auth._session_secret() == "derived:ch-secret"
    # 2回呼んでも同じ（=再起動してもCookieが生きる）
    assert auth._session_secret() == auth._session_secret()


def test_session_secret_prefers_explicit_value(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SECRET", "my-own-secret")
    assert auth._session_secret() == "my-own-secret"


def test_session_roundtrip_with_derived_secret(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SECRET", "")
    monkeypatch.setattr(settings, "LINE_LOGIN_CHANNEL_SECRET", "ch-secret")
    assert auth.read_session(auth.issue_session("U_X")) == "U_X"


def test_serializer_rejects_other_secret():
    """鍵が違えばCookieは読めない（=導出鍵でも署名検証が効いている）."""
    from itsdangerous import URLSafeSerializer, BadSignature
    a = URLSafeSerializer("derived:ch-secret", salt="web-session")
    b = URLSafeSerializer("derived:other", salt="web-session")
    token = a.dumps({"uid": "U_X"})
    assert a.loads(token)["uid"] == "U_X"
    with pytest.raises(BadSignature):
        b.loads(token)
