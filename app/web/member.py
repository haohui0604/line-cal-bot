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
from app.services import gym_db, periods
from app.services.dates import today_jst_date
from app.services import dates as jst_dates
from app.services.day_view import pfc_percent_series

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


def _minimal_request(path: str = "/"):
    """テスト等から直接呼ばれたとき用の最小 Request."""
    from starlette.requests import Request as _Req
    return _Req({"type": "http", "method": "GET", "path": path,
                 "raw_path": path.encode(), "headers": [], "query_string": b"",
                 "scheme": "http", "server": ("testserver", 80),
                 "client": ("test", 1), "root_path": ""})


def _render(request, name: str, ctx: dict = None):
    """Starlette の新旧どちらの TemplateResponse シグネチャでも描画する.

    新しい Starlette は TemplateResponse(request, name, context) の順なので、
    旧来の ("name", {...}) 呼び出しは name に dict が入り
    "unhashable type: 'dict'" で 500 になる。
    """
    import inspect
    ctx = dict(ctx or {})
    ctx.setdefault("request", request)
    try:
        params = list(inspect.signature(templates.TemplateResponse).parameters)
    except (TypeError, ValueError):
        params = []
    if params[:2] == ["request", "name"]:
        return templates.TemplateResponse(request, name, ctx)
    return templates.TemplateResponse(name, ctx)


def _verify_uid(id_token: str) -> str:
    """LIFFのIDトークンを検証して line_user_id を返す.

    失敗理由をレスポンスにも載せる。期限切れ（expired）は再ログインで直るが、
    audience 不一致などの設定ミスは何度ログインしても直らないため、
    フロントとログの両方で区別できるようにする。
    """
    try:
        claims = auth.verify_id_token(id_token)
    except Exception as e:
        error = getattr(e, "error", "") or type(e).__name__
        desc = getattr(e, "description", "")
        logger.warning("liff id_token verify failed: error=%s desc=%s",
                       error, desc)
        hint = "expired" if "expir" in desc.lower() else "invalid"
        raise HTTPException(
            status_code=401,
            detail=f"認証に失敗しました（{hint} / {error}）")
    uid = claims["sub"]
    gym_db.upsert_user(uid, claims.get("name"), claims.get("picture"))
    return uid


PAGE_DAYS = 14      # 1ページ＝14日
MAX_OFFSET = 3      # 何ページ前まで遡れるか


def _window(days: int, offset: int):
    """(start, end) を返す。offset=0 が直近、増えるほど過去へ遡る."""
    return periods.window(days, offset)


def _weight_stats(series):
    """期間内の体重から「現在値」と「期間初日からの増減」を出す（共通ロジック）."""
    return periods.weight_stats(series)


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
    nav = periods.period_nav(PAGE_DAYS, off)
    ctx = {"request": request, "liff_id": settings.LIFF_ID, "nav": nav}
    try:      # Starlette 0.29+ は (request, name, context)
        return templates.TemplateResponse(
            request=request, name="member_home.html", context=ctx)
    except TypeError:  # 旧Starletteは (name, context)
        return templates.TemplateResponse("member_home.html", ctx)


@router.post("/api/me/summary")
def me_summary(body: TokenIn):
    """自分のカロリー推移・体重推移（グラフ用JSON）.

    トレーナー画面と同じ periods.build_summary を使うので、
    現在値・増減・目標体重・目標摂取カロリーの扱いが両画面で一致する。
    """
    uid = _verify_uid(body.id_token)
    days = max(1, min(body.days, 90))
    offset = max(0, min(body.offset, 52))
    data = periods.build_summary(uid, days=days, offset=offset)
    data["today"] = fetch_day_summary(uid, today_jst_date().isoformat())
    try:
        data["share"] = gym_db.get_member_share_scope(uid)
    except Exception:
        data["share"] = None
    return data


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
def member_day_page(request: Request = None):
    """会員の日別ビュー。/me のサブパスに置くことでLIFFのエンドポイント配下とする.

    request はテストから直接呼べるように省略可能。
    """
    if request is None:
        request = _minimal_request("/me/day")
    return _render(request, "day_detail.html", {
        "member_id": None, "can_comment": False,
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
