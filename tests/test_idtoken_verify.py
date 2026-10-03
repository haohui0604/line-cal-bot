"""IDトークン検証失敗の切り分け（400の理由を保持できるか）."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DB_PATH", str(Path(tempfile.mkdtemp()) / "idtoken.db"))
os.environ.setdefault("TURSO_DATABASE_URL", "")

from app import auth  # noqa: E402


class _Resp:
    def __init__(self, status, body, text=""):
        self.status_code = status
        self._body = body
        self.text = text

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


def test_verify_failure_keeps_reason(monkeypatch):
    monkeypatch.setattr(auth.httpx, "post", lambda *a, **k: _Resp(
        400, {"error": "invalid_request",
              "error_description": "IdToken expired."}))
    with pytest.raises(auth.IdTokenError) as e:
        auth.verify_id_token("dummy")
    assert e.value.status == 400
    assert e.value.error == "invalid_request"
    assert "expired" in e.value.description.lower()


def test_verify_audience_mismatch_is_distinguishable(monkeypatch):
    monkeypatch.setattr(auth.httpx, "post", lambda *a, **k: _Resp(
        400, {"error": "invalid_request",
              "error_description": "Invalid IdToken Audience."}))
    with pytest.raises(auth.IdTokenError) as e:
        auth.verify_id_token("dummy")
    assert "audience" in e.value.description.lower()


def test_verify_non_json_body(monkeypatch):
    monkeypatch.setattr(auth.httpx, "post",
                        lambda *a, **k: _Resp(400, "<html>", "<html>oops"))
    with pytest.raises(auth.IdTokenError) as e:
        auth.verify_id_token("dummy")
    assert e.value.error == "non_json"


def test_verify_ok(monkeypatch):
    monkeypatch.setattr(auth.httpx, "post",
                        lambda *a, **k: _Resp(200, {"sub": "U1", "name": "n"}))
    assert auth.verify_id_token("dummy")["sub"] == "U1"


def test_member_verify_uid_maps_reason(monkeypatch):
    import app.web.member as m

    def boom(_t):
        raise auth.IdTokenError(400, "invalid_request", "IdToken expired.")

    monkeypatch.setattr(m.auth, "verify_id_token", boom)
    with pytest.raises(Exception) as e:
        m._verify_uid("dummy")
    assert "expired" in e.value.detail
