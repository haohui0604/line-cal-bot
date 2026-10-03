"""会員向けLIFF画面 (Phase 3).

会員はLINEのリッチメニューからLIFFアプリ(/me)を開き、
IDトークンで本人確認する（ログイン操作は不要）。
APIはCookieではなく毎回 id_token を検証する方式
（LINE内ブラウザやSafariのCookie制限を避けるため）。
"""
import logging
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
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
from app.services import dates as jst_dates
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


PAGE_DAYS = 14      # 1ページ＝14日
MAX_OFFSET = 3      # 何ページ前まで遡れるか


def _window(days: int, offset: int):
    """(start, end) を返す。offset=0 が直近、増えるほど過去へ遡る."""
    days = max(1, min(int(days), 90))
    offset = max(0, min(int(offset), 52))
    end = today_jst_date() - timedelta(days=offset * days)
    start = end - timedelta(days=days - 1)
    return start, end


def _weight_stats(series):
    """期間内の体重から「現在値」と「期間初日（14日前）からの増減」を出す.

    記録が無い日は fetch_weight_series が直前の値で繰越済みなので、
    14日前ちょうどの記録が無くても「その日以前の直近値」が入る。
    """
    vals = [(str(w.get("date") or "")[:10], w.get("weight_kg"))
            for w in (series or [])]
    vals = [(d, v) for d, v in vals if v is not None]
    if not vals:
        return {"current": None, "base": None, "delta": None,
                "current_date": None, "base_date": None}
    base_date, base = vals[0]
    cur_date, cur = vals[-1]
    return {"current": round(float(cur), 1), "base": round(float(base), 1),
            "delta": round(float(cur) - float(base), 1),
            "current_date": cur_date, "base_date": base_date}


class TokenIn(BaseModel):
    id_token: str
    days: int = 14
    offset: int = 0   # 週送り（0=直近14日, 1=その前の14日 ...）
    cm_offset: int = 0   # コメントのページング開始位置


@router.get("/me", response_class=HTMLResponse)
def member_home(request: Request, offset: int = 0):
    """LIFFのエンドポイント。LIFF_IDはテンプレートに埋め込む.

    期間ナビ（＜過去 / 未来＞）はサーバ側で描画する。
    offset が増えるほど過去へ遡る（offset=0 が直近14日）。
    """
    if not settings.LIFF_ID:
        raise HTTPException(status_code=503, detail="LIFFが未設定です")
    off = max(0, min(int(offset), MAX_OFFSET))
    start, end = _window(PAGE_DAYS, off)
    nav = {
        "offset": off,
        "max_offset": MAX_OFFSET,
        "past_href": f"?offset={off + 1}",                 # ＜ 過去
        "future_href": f"?offset={max(0, off - 1)}",       # 未来 ＞
        "show_past": off < MAX_OFFSET,
        "show_future": off > 0,
        "range": f"{start.isoformat()} 〜 {end.isoformat()}",
    }
    ctx = {"request": request, "liff_id": settings.LIFF_ID, "nav": nav}
    try:      # Starlette 0.29+ は (request, name, context)
        return templates.TemplateResponse(
            request=request, name="member_home.html", context=ctx)
    except TypeError:  # 旧Starletteは (name, context)
        return templates.TemplateResponse("member_home.html", ctx)


@router.post("/api/me/summary")
def me_summary(body: TokenIn):
    """自分のカロリー推移・体重推移（グラフ用JSON）."""
    uid = _verify_uid(body.id_token)
    days = max(1, min(body.days, 90))
    offset = max(0, min(body.offset, 52))
    labels, intake, burn, pfc = [], [], [], []
    today = today_jst_date()
    start, end = _window(days, offset)
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        s = fetch_day_summary(uid, d)
        labels.append(d[5:])
        intake.append(s.get("intake_kcal") or 0)
        burn.append(s.get("burn_kcal") or s.get("burn") or 0)
        pfc.append((s.get("protein_g"), s.get("fat_g"), s.get("carb_g")))
    w_all = fetch_weight_series(uid, days=days * (offset + 1)) or []
    _s, _e = start.isoformat(), end.isoformat()
    wseries = [w for w in w_all if _s <= str(w.get("date") or "")[:10] <= _e]
    wst = _weight_stats(wseries)
    return {
        "labels": labels, "intake": intake, "burn": burn,
        "window_start": _s, "window_end": _e, "offset": offset,
        "weight_labels": [(w.get("date") or "")[5:] for w in wseries],
        "weight": [w.get("weight_kg") for w in wseries],
        "weight_current": wst["current"],
        "weight_base": wst["base"],
        "weight_delta": wst["delta"],
        "weight_current_date": wst["current_date"],
        "weight_base_date": wst["base_date"],
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
    """自分宛のコメント（トレーナー / AIコーチ）を30件ずつ返す."""
    uid = _verify_uid(body.id_token)
    lim = 30
    off = max(0, min(body.cm_offset, 5000))
    items = gym_db.fetch_comments_for_user(uid, limit=lim, offset=off)
    total = gym_db.count_comments_for_user(uid)
    return {"comments": items, "offset": off, "limit": lim, "total": total,
            "unread": gym_db.count_unread_comments_for_member(uid),
            "has_more": off + len(items) < total}


@router.post("/api/me/role")
def me_role(body: TokenIn):
    """ログイン中の会員がスタッフ(トレーナー/管理者)かどうかを返す."""
    uid = _verify_uid(body.id_token)
    return {"is_staff": gym_db.is_staff(uid)}


@router.get("/me/day", response_class=HTMLResponse)
def member_day_page():
    """会員の日別ビュー。/me のサブパスに置くことでLIFFのエンドポイント配下とする."""
    return templates.TemplateResponse("day_detail.html", {
        "request": {}, "member_id": None, "can_comment": False,
        "can_edit": True,
        "liff_id": settings.LIFF_ID,
        "initial_date": jst_dates.yesterday_jst(),
    })


# ================= Phase 8: 会員の既読 / 共有設定 =================

@router.post("/api/me/comments/read")
def me_comments_read(body: dict):
    """コメントを開いた時点で既読にする（新着バッジの消し込み）."""
    uid = _verify_uid((body or {}).get("id_token") or "")
    marked = gym_db.mark_member_comments_read(uid, (body or {}).get("up_to_id"))
    return {"ok": True, "marked": marked,
            "unread": gym_db.count_unread_comments_for_member(uid)}


@router.post("/api/me/share-scope")
def me_share_scope(body: dict):
    """記録の共有範囲（assigned=担当のみ / gym=同一ジムのスタッフ全員）.

    scope を省略すると現在値を返す。変更時は同意日時を記録する。
    """
    b = body or {}
    uid = _verify_uid(b.get("id_token") or "")
    if "scope" not in b:
        return {"share": gym_db.get_member_share_scope(uid),
                "unread": gym_db.count_unread_comments_for_member(uid)}
    res = gym_db.set_member_share_scope(uid, b.get("scope"))
    if not res.get("ok"):
        raise HTTPException(status_code=400,
                            detail="共有設定を変更できませんでした（ジム未加入の可能性）")
    return {"ok": True, "share": gym_db.get_member_share_scope(uid)}
