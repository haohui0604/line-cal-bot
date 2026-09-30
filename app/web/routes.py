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
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app import auth
from app.config import settings
from app.services import gym_db
from app.services.gym_db import (
    upsert_user, get_membership_for_user, is_staff,
)

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

# OAuth state の簡易保持（無料枠=単一プロセス前提。再起動で消えるが許容）
_pending_states: set = set()


@router.get("/")
def index(request: Request):
    """入口: ログイン済みスタッフは管理画面へ、それ以外はログインへ."""
    uid = auth.current_user_id(request)
    if uid and is_staff(uid):
        return RedirectResponse("/trainer")
    return RedirectResponse("/login")


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

    # ロールで遷移先を振り分け（スタッフ→管理画面、会員→Phase 3 までは状態表示）
    dest = "/trainer" if is_staff(uid) else "/api/me"
    resp = RedirectResponse(dest)
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


# ---- トレーナー招待 (Phase 3.5: コード方式) ----

@router.get("/trainer-invite", response_class=__import__("fastapi.responses", fromlist=["HTMLResponse"]).HTMLResponse)
def trainer_invite_page(request: Request):
    uid = auth.current_user_id(request)
    if not uid:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/login")
    return templates.TemplateResponse("trainer_invite.html", {"request": request})


@router.post("/api/trainer-invite")
def trainer_invite_use(request: Request, body: dict):
    uid = auth.current_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="ログインが必要です")
    code = (body.get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="コードを入力してください")
    res = gym_db.use_trainer_invite(code, uid)
    if res["result"] == "invalid":
        raise HTTPException(status_code=404, detail="コードが無効です")
    if res["result"] == "used":
        raise HTTPException(status_code=409, detail="このコードは使用済みです")
    if res["result"] == "expired":
        raise HTTPException(status_code=410, detail="コードの有効期限切れです")
    return res


# ---- ジム管理（オーナー）: 招待コード発行 ----

@router.get("/admin/gym", response_class=__import__("fastapi.responses", fromlist=["HTMLResponse"]).HTMLResponse)
def admin_gym(request: Request):
    uid = auth.current_user_id(request)
    if not uid:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/login")
    admin_ms = [m for m in gym_db.get_staff_memberships(uid) if m["role"] == "gym_admin"]
    if not admin_ms:
        raise HTTPException(status_code=403, detail="ジム管理者権限がありません")
    for g in admin_ms:
        g["members"] = gym_db.list_members_for_gym(g["gym_id"])
    # request を第1引数で渡す（新しい Starlette でも動く書き方）
    return templates.TemplateResponse(
        request, "admin_gym.html", {"gyms": admin_ms})


@router.post("/api/admin/gym/invite")
def admin_gym_invite(request: Request, body: dict):
    uid = auth.current_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="ログインが必要です")
    gym_id = int(body.get("gym_id") or 0)
    admin = [m for m in gym_db.get_staff_memberships(uid)
             if m["role"] == "gym_admin" and m["gym_id"] == gym_id]
    if not admin:
        raise HTTPException(status_code=403, detail="このジムの管理者ではありません")
    code = gym_db.create_trainer_invite(gym_id, uid)
    return {"code": code}


# ---- ジム管理（オーナー）: 会員の解除 ----

def _push_to_user(line_user_id: str, text: str) -> None:
    """LINE push（未設定・失敗時は黙って続行）."""
    if not settings.LINE_CHANNEL_ACCESS_TOKEN:
        return
    try:
        from linebot import LineBotApi
        from linebot.models import TextSendMessage
        LineBotApi(settings.LINE_CHANNEL_ACCESS_TOKEN).push_message(
            line_user_id, TextSendMessage(text=text))
    except Exception:
        logger.exception("push to user failed")


@router.post("/api/admin/gym/member/remove")
def admin_gym_member_remove(request: Request, body: dict):
    """オーナーが会員をジムから解除する（status='left'）。"""
    uid = auth.current_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="ログインが必要です")
    membership_id = int(body.get("membership_id") or 0)
    res = gym_db.remove_member(membership_id, by_user_id=uid)
    if res["result"] == "not_found":
        raise HTTPException(status_code=404, detail="会員が見つかりません")
    if res["result"] == "forbidden":
        raise HTTPException(status_code=403, detail="このジムの管理者ではありません")
    _push_to_user(
        res["member_id"],
        f"🏋️「{res.get('gym_name') or 'ジム'}」から退出しました。\n"
        "これまでの記録データはそのまま残っています。\n"
        "別のジムに入るには、そのジムの入会コード（GYM-XXXX）を"
        "このトークに送ってください。")
    return {"ok": True}
