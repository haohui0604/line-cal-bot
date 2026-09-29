"""LINEログイン (OAuth2) とセッションCookie.

重要: Messaging APIチャンネルと同じプロバイダー配下に
LINEログインチャンネルを作れば userId が一致するため、
「Webの人 = LINEのどの人か」の紐づけコードは不要。
"""
import secrets
from typing import Optional
from urllib.parse import urlencode

import httpx
from itsdangerous import URLSafeSerializer, BadSignature
from fastapi import Request

from app.config import settings

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


def verify_id_token(id_token: str) -> dict:
    """id_token を検証し {"sub": line_user_id, "name": ..., "picture": ...} を返す.
    LIFFのIDトークンも同じログインチャンネルならこの関数で検証できる."""
    resp = httpx.post(VERIFY_URL, data={
        "id_token": id_token,
        "client_id": settings.LINE_LOGIN_CHANNEL_ID,
    }, timeout=10)
    resp.raise_for_status()
    return resp.json()


# ---- セッションCookie（署名付き・DB不要） ----

def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(settings.SESSION_SECRET, salt="web-session")


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
