"""会員向けLIFF画面 (Phase 3).

会員はLINEのリッチメニューからLIFFアプリ(/me)を開き、
IDトークンで本人確認する（ログイン操作は不要）。
APIはCookieではなく毎回 id_token を検証する方式
（LINE内ブラウザやSafariのCookie制限を避けるため）。
"""
import logging
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import auth
from app.config import settings
from app.services.db import (
    fetch_day_summary, fetch_recent_entries, fetch_weight_series,
)
from app.services import gym_db
from app.services.dates import today_jst_date
from app.services.day_view import pfc_percent_series

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


def _verify_uid(id_token: str) -> str:
    """LIFFのIDトークンを検証して line_user_id を返す."""
    try:
        claims = auth.verify_id_token(id_token)
    except Exception:
        logger.warning("liff id_token verify failed", exc_info=True)
        raise HTTPException(status_code=401, detail="認証に失敗しました")
    uid = claims["sub"]
    gym_db.upsert_user(uid, claims.get("name"), claims.get("picture"))
    return uid


class TokenIn(BaseModel):
    id_token: str
    days: int = 14


@router.get("/me", response_class=HTMLResponse)
def member_home():
    """LIFFのエンドポイント。LIFF_IDはテンプレートに埋め込む."""
    if not settings.LIFF_ID:
        raise HTTPException(status_code=503, detail="LIFFが未設定です")
    return templates.TemplateResponse(
        "member_home.html", {"request": {}, "liff_id": settings.LIFF_ID})


@router.post("/api/me/summary")
def me_summary(body: TokenIn):
    """自分のカロリー推移・体重推移（グラフ用JSON）."""
    uid = _verify_uid(body.id_token)
    days = max(1, min(body.days, 90))
    labels, intake, burn, pfc = [], [], [], []
    today = today_jst_date()
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        s = fetch_day_summary(uid, d)
        labels.append(d[5:])
        intake.append(s.get("intake_kcal") or 0)
        burn.append(s.get("burn_kcal") or s.get("burn") or 0)
        pfc.append((s.get("protein_g"), s.get("fat_g"), s.get("carb_g")))
    wseries = fetch_weight_series(uid, days=days) or []
    return {
        "labels": labels, "intake": intake, "burn": burn,
        "weight_labels": [(w.get("date") or "")[5:] for w in wseries],
        "weight": [w.get("weight_kg") for w in wseries],
        "today": fetch_day_summary(uid, today.isoformat()),
        **pfc_percent_series(pfc),
    }


@router.post("/api/me/history")
def me_history(body: TokenIn):
    """自分の最近の記録."""
    uid = _verify_uid(body.id_token)
    return {"entries": fetch_recent_entries(uid, limit=50)}


@router.post("/api/me/comments")
def me_comments(body: TokenIn):
    """自分宛のコメント（トレーナー / AIコーチ）."""
    uid = _verify_uid(body.id_token)
    return {"comments": gym_db.fetch_comments_for_user(uid, limit=30)}


@router.get("/me/day", response_class=HTMLResponse)
def member_day_page():
    """会員の日別ビュー。/me のサブパスに置くことでLIFFのエンドポイント配下とする."""
    return templates.TemplateResponse("day_detail.html", {
        "request": {}, "member_id": None, "can_comment": False,
        "liff_id": settings.LIFF_ID, "initial_date": "",
    })
