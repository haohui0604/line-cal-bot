"""LINEログイン (OAuth2) とセッションCookie.

重要: Messaging APIチャンネルと同じプロバイダー配下に
LINEログインチャンネルを作れば userId が一致するため、
「Webの人 = LINEのどの人か」の紐づけコードは不要。
"""
import logging
import secrets
from typing import Optional
from urllib.parse import urlencode

import httpx
from itsdangerous import URLSafeSerializer, BadSignature
from fastapi import Request

from app.config import settings

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://access.line.me/oauth2/v2.1/authorize"
TOKEN_URL = "https://api.line.me/oauth2/v2.1/token"
VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"

SESSION_COOKIE = "session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30日


def login_configured() -> bool:
    return bool(settings.LINE_LOGIN_CHANNEL_ID
                and settings.LINE_LOGIN_CHANNEL_SECRET)


def build_login_url(state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": settings.LINE_LOGIN_CHANNEL_ID,
        "redirect_uri": settings.BASE_URL.rstrip("/") + "/auth/callback",
        "state": state,
        "scope": "openid profile",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def new_state() -> str:
    return secrets.token_urlsafe(16)


def exchange_code(code: str) -> dict:
    resp = httpx.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.BASE_URL.rstrip("/") + "/auth/callback",
        "client_id": settings.LINE_LOGIN_CHANNEL_ID,
        "client_secret": settings.LINE_LOGIN_CHANNEL_SECRET,
    }, timeout=10)
    resp.raise_for_status()
    return resp.json()


class IdTokenError(Exception):
    """LINEのIDトークン検証に失敗した（理由つき）."""

    def __init__(self, status: int, error: str = "", description: str = ""):
        self.status = status
        self.error = error
        self.description = description
        super().__init__(f"verify {status}: {error} / {description}")


def verify_id_token(id_token: str) -> dict:
    """id_token を検証し {"sub": line_user_id, "name": ..., "picture": ...} を返す.

    LIFFのIDトークンも同じログインチャンネルならこの関数で検証できる。
    失敗時は LINE が返す error / error_description を保持して IdTokenError を投げる。
    400 の理由（IdToken expired / Invalid IdToken Audience 等）を必ずログに残し、
    「再ログインで直るのか、設定ミスなのか」を切り分けられるようにする。
    """
    resp = httpx.post(VERIFY_URL, data={
        "id_token": id_token,
        "client_id": settings.LINE_LOGIN_CHANNEL_ID,
    }, timeout=10)
    if resp.status_code != 200:
        try:
            body = resp.json()
        except Exception:
            body = {"error": "non_json",
                    "error_description": (resp.text or "")[:300]}
        error = str(body.get("error") or "")
        desc = str(body.get("error_description") or "")
        logger.error("LINE verify failed: status=%s client_id=%s error=%s "
                     "desc=%s token_len=%s",
                     resp.status_code, settings.LINE_LOGIN_CHANNEL_ID,
                     error, desc, len(id_token or ""))
        raise IdTokenError(resp.status_code, error, desc)
    return resp.json()


# ---- セッションCookie（署名付き・DB不要） ----

def _session_secret() -> str:
    """セッションCookieの署名鍵.

    SESSION_SECRET を設定していないと、プロセス再起動のたびに鍵が変わって
    Cookie が全部無効になる（→ 毎回ログインに飛ばされ、その先で詰まる）。
    そのため未設定なら LINE のチャネルシークレットから決定的に導出する。
    """
    secret = getattr(settings, "SESSION_SECRET", "") or ""
    if secret:
        return secret
    base = getattr(settings, "LINE_LOGIN_CHANNEL_SECRET", "") or ""
    if base:
        logger.warning("SESSION_SECRET 未設定。LINEチャネルシークレットから"
                       "セッション鍵を導出します（再起動でもCookieが維持されます）")
        return "derived:" + base
    logger.warning("SESSION_SECRET / LINE_LOGIN_CHANNEL_SECRET が共に未設定。"
                   "セッションはプロセス再起動で無効になります")
    return "insecure-default-session-secret"


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(_session_secret(), salt="web-session")


def issue_session(line_user_id: str) -> str:
    return _serializer().dumps({"uid": line_user_id})


def read_session(raw: str) -> Optional[str]:
    try:
        return _serializer().loads(raw).get("uid")
    except BadSignature:
        return None


def current_user_id(request: Request) -> Optional[str]:
    raw = request.cookies.get(SESSION_COOKIE)
    return read_session(raw) if raw else None
