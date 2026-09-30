"""日別ビュー (Phase 3.5): 会員LIFF / トレーナーWeb 共用.

- トレーナー: /day?member_id=U... をCookieセッションで開く（コメント投稿可）
- 会員: LIFF内で /day を開き id_token で本人確認（自分の記録の編集可）
"""
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from linebot import LineBotApi
from linebot.models import TextSendMessage
from pydantic import BaseModel

from app import auth
from app.config import settings
from app.services import gym_db
from app.services.coach import get_day_comment
from app.services import dates as jst_dates
from app.services.day_view import (
    build_day_data, add_entry_manual, update_entry_full,
)
from app.services.db import (
    delete_entry, fetch_entry_for_user, update_entry_note,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# 記録区分（DB の meal_slot に入る許可値）
ALLOWED_SLOTS = ("breakfast", "lunch", "dinner", "snack", "night")
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

line_bot_api = (
    LineBotApi(settings.LINE_CHANNEL_ACCESS_TOKEN)
    if settings.LINE_CHANNEL_ACCESS_TOKEN else None
)


def _resolve_viewer(request: Request, member_id: Optional[str],
                    id_token: Optional[str]):
    """(対象user_id, is_trainer, viewer_id) を返す."""
    if member_id:
        staff_id = auth.current_user_id(request)
        if not staff_id:
            raise HTTPException(status_code=401, detail="ログインが必要です")
        if not gym_db.can_staff_view_member(staff_id, member_id):
            raise HTTPException(status_code=403, detail="権限がありません")
        return member_id, True, staff_id
    if id_token:
        try:
            claims = auth.verify_id_token(id_token)
        except Exception:
            raise HTTPException(status_code=401, detail="認証に失敗しました")
        uid = claims["sub"]
        gym_db.upsert_user(uid, claims.get("name"), claims.get("picture"))
        return uid, False, uid
    raise HTTPException(status_code=401, detail="認証情報がありません")


def _push_to_member(member_id: str, text: str) -> None:
    if not line_bot_api:
        return
    try:
        line_bot_api.push_message(member_id, TextSendMessage(text=text))
    except Exception:
        logger.exception("push to member failed")


# ---- ページ ----

@router.get("/day", response_class=HTMLResponse)
def day_page(request: Request, member_id: str = "", date: str = ""):
    if member_id:
        staff = auth.current_user_id(request)
        if not staff:
            return RedirectResponse("/login")
        if not gym_db.can_staff_view_member(staff, member_id):
            raise HTTPException(status_code=403,
                                detail="この会員を表示する権限がありません")
        can_comment = True
    else:
        can_comment = False
    # トレーナーは閲覧とコメントのみ。記録の追加/修正/削除は会員本人だけ
    can_edit = not bool(member_id)
    return templates.TemplateResponse("day_detail.html", {
        "request": request,
        "member_id": member_id or None,
        "can_comment": can_comment,
        "can_edit": can_edit,
        "liff_id": settings.LIFF_ID,
        # 初期表示は前日（?date= の指定があればそれを優先）
        "initial_date": date or jst_dates.yesterday_jst(),
    })


# ---- データAPI ----

class DayDataIn(BaseModel):
    date: Optional[str] = None        # null でも受ける（初回ロード時 422 対策）
    member_id: Optional[str] = None
    id_token: Optional[str] = None

    class Config:
        extra = "ignore"


@router.post("/api/day/data")
def day_data(body: DayDataIn, request: Request):
    uid, is_trainer, _viewer = _resolve_viewer(
        request, body.member_id, body.id_token)
    # date 未指定なら前日（JST）。?date= 指定は上書きされる
    target = body.date or jst_dates.yesterday_jst()
    data = build_day_data(uid, target)
    data["ai_comment"] = get_day_comment(uid, target)
    data["trainer_comments"] = gym_db.fetch_comments_for_date(uid, target)
    return data


# ---- 記録の追加/修正/削除 ----

class EntryOpIn(BaseModel):
    action: str                       # add / edit / delete
    # （member_id/id_token 等は下に定義）

    class Config:
        extra = "ignore"
    member_id: Optional[str] = None
    id_token: Optional[str] = None
    entry_id: Optional[int] = None
    date: Optional[str] = None
    meal_slot: Optional[str] = None
    food_name: Optional[str] = None
    kcal: Optional[float] = None
    protein_g: Optional[float] = None
    fat_g: Optional[float] = None
    carb_g: Optional[float] = None
    salt_g: Optional[float] = None
    body: Optional[str] = None   # memo/question の本文


@router.post("/api/day/entries")
def day_entry_ops(body: EntryOpIn, request: Request):
    uid, is_trainer, _viewer = _resolve_viewer(
        request, body.member_id, body.id_token)
    if is_trainer:
        raise HTTPException(
            status_code=403,
            detail="記録の追加・修正・削除は会員本人のみ行えます"
                   "（トレーナーは閲覧とコメントのみ）")
    source = "user_report"

    if body.action == "add":
        if not (body.date and body.meal_slot and body.food_name):
            raise HTTPException(status_code=400,
                                detail="日付・区分・食品名は必須です"
                                       "（kcalは空欄ならAIが推定します）")
        try:
            return add_entry_manual(
                uid, date=body.date, meal_slot=body.meal_slot,
                food_name=body.food_name.strip(), kcal=body.kcal,
                protein_g=body.protein_g, fat_g=body.fat_g,
                carb_g=body.carb_g, salt_g=body.salt_g, source=source)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    if body.action == "edit":
        if not (body.entry_id and body.meal_slot and body.food_name
                and body.kcal is not None):
            raise HTTPException(status_code=400, detail="必須項目が不足しています")
        # 区分は許可値のみ
        if body.meal_slot not in ALLOWED_SLOTS:
            raise HTTPException(
                status_code=400,
                detail="区分は 朝食/昼食/夕食/間食/夜食 から選んでください")
        # 日付は実在する日付・未来日不可（未指定なら日付は変更しない）
        new_date = (body.date or "").strip() or None
        if new_date:
            try:
                parsed = datetime.strptime(new_date, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail="日付は YYYY-MM-DD 形式で指定してください")
            if parsed > jst_dates.today_jst_date():
                raise HTTPException(
                    status_code=400, detail="未来の日付には変更できません")
            new_date = parsed.isoformat()
        try:
            ok = update_entry_full(
                uid, body.entry_id, meal_slot=body.meal_slot,
                food_name=body.food_name.strip(), kcal=body.kcal,
                protein_g=body.protein_g, fat_g=body.fat_g,
                carb_g=body.carb_g, salt_g=body.salt_g, date=new_date)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not ok:
            # 所有者チェック: 他人の entry_id / 存在しない id はここで 404
            raise HTTPException(status_code=404, detail="記録が見つかりません")
        return {"ok": True, "date": new_date}

    if body.action == "memo":
        # 自分メモ（本人のみ。entries.note に保存）
        if not body.entry_id:
            raise HTTPException(status_code=400, detail="entry_id が必要です")
        ok = update_entry_note(uid, body.entry_id, (body.body or "").strip())
        if not ok:
            raise HTTPException(status_code=404, detail="記録が見つかりません")
        return {"ok": True}

    if body.action == "question":
        # トレーナーへの質問（本人のみ。コメントとして保存し担当Tへ通知）
        if not body.entry_id:
            raise HTTPException(status_code=400, detail="entry_id が必要です")
        text = (body.body or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="質問内容が空です")
        entry = fetch_entry_for_user(uid, body.entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail="記録が見つかりません")
        qdate = entry.get("date") or body.date or jst_dates.today_jst()
        gym_db.add_comment(
            user_id=uid,
            body=f"❓ {entry['food_name']}（{qdate}）についての質問\n{text}",
            author_type="member", author_id=uid,
            target_date=qdate, is_directive=False)
        trainer_id = gym_db.get_member_trainer(uid)
        if trainer_id:
            me = gym_db.get_user(uid) or {}
            _push_to_member(
                trainer_id,
                f"❓ {me.get('display_name') or '会員'} さんから質問が届きました\n"
                f"{qdate} / {entry['food_name']}\n\n{text}")
        return {"ok": True, "pushed": bool(trainer_id)}

    if body.action == "delete":
        if not body.entry_id:
            raise HTTPException(status_code=400, detail="entry_id が必要です")
        ok = delete_entry(uid, body.entry_id)
        if not ok:
            raise HTTPException(status_code=404, detail="記録が見つかりません")
        return {"ok": True}

    raise HTTPException(status_code=400, detail="不明な action です")


# ---- 日単位コメント（トレーナーのみ） ----

class CommentIn(BaseModel):
    date: str
    body: str
    is_directive: bool = False
    member_id: Optional[str] = None
    id_token: Optional[str] = None


@router.post("/api/day/comment")
def post_day_comment(body: CommentIn, request: Request):
    uid, is_trainer, viewer = _resolve_viewer(
        request, body.member_id, body.id_token)
    if not is_trainer:
        raise HTTPException(status_code=403,
                            detail="コメントはトレーナーのみ投稿できます")
    text = (body.body or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="コメントが空です")
    gym_db.add_comment(
        user_id=uid, body=text, author_type="trainer", author_id=viewer,
        target_date=body.date, is_directive=body.is_directive)
    staff = gym_db.get_user(viewer) or {}
    msg = (f"💬 {staff.get('display_name') or 'トレーナー'}"
           f" から{body.date}の記録にコメントが届きました\n\n{text}")
    if body.is_directive:
        msg += "\n\n⭐ この内容はAIコーチの今後のアドバイスにも反映されます"
    _push_to_member(uid, msg)
    return {"ok": True}
