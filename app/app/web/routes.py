"""Web側ルート: LINEログイン / LIFF認証.

Phase 1 のスコープ:
  - /login, /auth/callback, /logout  … トレーナー・PC用のLINEログイン
  - /api/liff/auth                   … 会員用LIFFのIDトークン検証
  - /api/me                          … 動作確認用（ログイン中ユーザーの状態）
画面（トレーナーダッシュボード等）は Phase 2 で追加する。
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app import auth
from app.services.gym_db import upsert_user, get_membership_for_user

logger = logging.getLogger(__name__)
router = APIRouter()

# OAuth state の簡易保持（無料枠=単一プロセス前提。再起動で消えるが許容）
_pending_states: set = set()


@router.get("/login")
def login():
    if not auth.login_configured():
        raise HTTPException(status_code=503,
                            detail="LINEログインが未設定です")
    state = auth.new_state()
    _pending_states.add(state)
    return RedirectResponse(auth.build_login_url(state))


@router.get("/auth/callback")
def auth_callback(code: str = "", state: str = "",
                  error: str = "", error_description: str = ""):
    if error:
        raise HTTPException(status_code=400,
                            detail=f"LINEログインがキャンセルされました: {error}")
    if state not in _pending_states:
        raise HTTPException(status_code=400, detail="state が不正です。やり直してください")
    _pending_states.discard(state)

    tokens = auth.exchange_code(code)
    claims = auth.verify_id_token(tokens["id_token"])
    uid = claims["sub"]
    upsert_user(uid, claims.get("name"), claims.get("picture"))

    resp = RedirectResponse("/api/me")  # Phase 2 でロール別画面に振り分ける
    resp.set_cookie(auth.SESSION_COOKIE, auth.issue_session(uid),
                    httponly=True, samesite="lax", secure=True,
                    max_age=auth.SESSION_MAX_AGE)
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/login")
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


class LiffAuthIn(BaseModel):
    id_token: str


@router.post("/api/liff/auth")
def liff_auth(body: LiffAuthIn):
    """LIFFから送られたIDトークンを検証し、ユーザー情報と所属を返す."""
    if not auth.login_configured():
        raise HTTPException(status_code=503,
                            detail="LINEログインが未設定です")
    claims = auth.verify_id_token(body.id_token)
    uid = claims["sub"]
    upsert_user(uid, claims.get("name"), claims.get("picture"))
    return {
        "line_user_id": uid,
        "display_name": claims.get("name"),
        "membership": get_membership_for_user(uid),
    }


@router.get("/api/me")
def api_me(request: Request):
    """動作確認用: Cookieセッションでログイン中ユーザーを返す."""
    uid = auth.current_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="未ログインです")
    return {"line_user_id": uid, "membership": get_membership_for_user(uid)}
