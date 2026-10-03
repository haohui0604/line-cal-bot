"""トレーナー / ジム管理者向けWeb画面 (Phase 2).

技術方針: SPAは使わず FastAPI + Jinja2 のサーバーレンダリング。
フォーム送信は通常の POST→リダイレクト(PRG)、グラフ描画だけ Chart.js(CDN)。
"""
import logging
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from linebot import LineBotApi
from linebot.models import TextSendMessage

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


def _render(request: Request, name: str, ctx: dict = None):
    """Starlette の新旧どちらの TemplateResponse シグネチャでも描画する."""
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

line_bot_api = (
    LineBotApi(settings.LINE_CHANNEL_ACCESS_TOKEN)
    if settings.LINE_CHANNEL_ACCESS_TOKEN else None
)


def _require_staff(request: Request) -> str:
    """ログイン済み & trainer/gym_admin であることを要求。満たさなければ例外."""
    uid = auth.current_user_id(request)
    if not uid:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    if not gym_db.is_staff(uid):
        raise HTTPException(status_code=403,
                            detail="トレーナー権限がありません")
    return uid


def _require_member_access(staff_id: str, member_id: str) -> None:
    if not gym_db.can_staff_view_member(staff_id, member_id):
        raise HTTPException(status_code=403,
                            detail="この会員を表示する権限がありません")


def _push_to_member(member_id: str, text: str) -> None:
    """会員のLINEに通知。失敗しても本体処理は成功扱いにする."""
    if not line_bot_api:
        return
    try:
        line_bot_api.push_message(member_id, TextSendMessage(text=text))
    except Exception:
        logger.exception("push to member failed")


# ---- 会員一覧 ----

@router.get("/trainer", response_class=HTMLResponse)
def trainer_home(request: Request):
    staff_id = _require_staff(request)
    today = today_jst_date().isoformat()
    rows = []
    for m in gym_db.list_members_for_staff(staff_id):
        latest = fetch_recent_entries(m["user_id"], limit=1)
        s = fetch_day_summary(m["user_id"], today)
        rows.append({
            **m,
            "today_kcal": s.get("intake_kcal") or 0,
            "last_record_date": latest[0]["date"] if latest else None,
        })
    pending = gym_db.list_pending_for_staff(staff_id)
    return _render(request, "staff_members.html", {
        "request": request, "members": rows, "pending_count": len(pending),
        "unread": gym_db.count_unread_member_comments(staff_id),
    })


# ---- 入会申請 ----

@router.get("/trainer/requests", response_class=HTMLResponse)
def requests_page(request: Request):
    staff_id = _require_staff(request)
    reqs = gym_db.list_pending_for_staff(staff_id)
    return _render(request, "staff_requests.html", {
        "request": request, "requests": reqs,
        "unread": gym_db.count_unread_member_comments(staff_id),
    })


@router.post("/trainer/requests/{membership_id}/approve")
def approve(request: Request, membership_id: int):
    staff_id = _require_staff(request)
    req = gym_db.get_request_for_staff(staff_id, membership_id)
    if not req:
        raise HTTPException(status_code=404,
                            detail="申請が見つかりません（すでに処理済みの可能性があります）")
    gym_db.approve_request(membership_id, trainer_id=staff_id)
    _push_to_member(
        req["user_id"],
        f"🎉 「{req['gym_name']}」への登録が完了しました！\n"
        "これから食事・運動の記録をよろしくお願いします。")
    return RedirectResponse("/trainer/requests", status_code=303)


@router.post("/trainer/requests/{membership_id}/reject")
def reject(request: Request, membership_id: int):
    staff_id = _require_staff(request)
    req = gym_db.get_request_for_staff(staff_id, membership_id)
    if not req:
        raise HTTPException(status_code=404,
                            detail="申請が見つかりません（すでに処理済みの可能性があります）")
    gym_db.reject_request(membership_id)
    return RedirectResponse("/trainer/requests", status_code=303)


# ---- 会員詳細 ----

@router.get("/trainer/members/{member_id}", response_class=HTMLResponse)
def member_detail(request: Request, member_id: str, cm_offset: int = 0):
    staff_id = _require_staff(request)
    _require_member_access(staff_id, member_id)
    u = gym_db.get_user(member_id) or {}
    today = today_jst_date().isoformat()
    return _render(request, "staff_member_detail.html", {
        "request": request,
        "member_id": member_id,
        "member_name": u.get("display_name") or member_id,
        "summary": fetch_day_summary(member_id, today),
        "entries": fetch_recent_entries(member_id, limit=30),
        "comments": gym_db.fetch_comments_for_user(member_id, limit=30,
                                                   offset=max(0, cm_offset)),
        "cm_offset": max(0, cm_offset),
        "cm_total": gym_db.count_comments_for_user(member_id),
        "cm_has_more": gym_db.count_comments_for_user(member_id) > max(0, cm_offset) + 30,
        "today": today,
    })


@router.post("/trainer/members/{member_id}/comments")
def post_comment(request: Request, member_id: str,
                 body: str = Form(...), is_directive: str = Form(default="")):
    staff_id = _require_staff(request)
    _require_member_access(staff_id, member_id)
    body = (body or "").strip()
    if body:
        directive = (is_directive == "on")
        gym_db.add_comment(
            user_id=member_id, body=body, author_type="trainer",
            author_id=staff_id, target_date=today_jst_date().isoformat(),
            is_directive=directive)
        staff = gym_db.get_user(staff_id) or {}
        text = (f"💬 {staff.get('display_name') or 'トレーナー'}"
                f" からコメントが届きました\n\n{body}")
        if directive:
            text += "\n\n⭐ この内容はAIコーチの今後のアドバイスにも反映されます"
        _push_to_member(member_id, text)
    return RedirectResponse(f"/trainer/members/{member_id}", status_code=303)


# ---- グラフ用JSON ----

@router.get("/api/trainer/members/{member_id}/summary")
def member_summary_api(request: Request, member_id: str, days: int = 14):
    staff_id = _require_staff(request)
    _require_member_access(staff_id, member_id)
    days = max(1, min(days, 90))
    labels, intake, burn, pfc = [], [], [], []
    today = today_jst_date()
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        s = fetch_day_summary(member_id, d)
        labels.append(d[5:])  # MM-DD
        intake.append(s.get("intake_kcal") or 0)
        burn.append(s.get("burn_kcal") or s.get("burn") or 0)
        pfc.append((s.get("protein_g"), s.get("fat_g"), s.get("carb_g")))
    wseries = fetch_weight_series(member_id, days=days) or []
    return {
        "labels": labels, "intake": intake, "burn": burn,
        "weight_labels": [(w.get("date") or "")[5:] for w in wseries],
        "weight": [w.get("weight_kg") for w in wseries],
        **pfc_percent_series(pfc),
    }


# ================= ジム管理者画面 (Phase 5) =================

def _admin_gyms(uid: str):
    return gym_db.list_admin_gyms(uid)


def _require_gym_admin(request: Request, gym_id: int = 0):
    """ジム管理者であることを要求し (uid, gym_id) を返す."""
    uid = _require_staff(request)
    admins = _admin_gyms(uid)
    if not admins:
        raise HTTPException(status_code=403, detail="ジム管理者権限がありません")
    if gym_id:
        if not any(int(a["gym_id"]) == int(gym_id) for a in admins):
            raise HTTPException(status_code=403, detail="このジムの管理者ではありません")
        return uid, int(gym_id)
    return uid, int(admins[0]["gym_id"])


@router.get("/gym", response_class=HTMLResponse)
def gym_home(request: Request, gym_id: int = 0, mine: int = 0):
    uid, gid = _require_gym_admin(request, gym_id)
    gym = gym_db.get_gym(gid) or {}
    return _render(request, "gym_admin.html", {
        "request": request,
        "me": uid,
        "gym": gym,
        "admin_gyms": _admin_gyms(uid),
        "staff": gym_db.list_gym_staff_with_status(gid),
        "trainers": [t for t in gym_db.list_gym_staff_with_status(gid)
                     if t["status"] == "active"],
        "members": gym_db.list_members_for_gym_filtered(gid, uid if mine else None),
        "member_count": len(gym_db.list_members_for_gym_filtered(gid)),
        "mine": mine,
        "pending": gym_db.list_pending_requests(gid),
        "my_comments": gym_db.list_my_member_comments(uid, limit=30),
        "my_comment_total": gym_db.count_my_member_comments(uid),
        "unread": gym_db.count_unread_member_comments(uid),
        "threads": gym_db.list_member_threads(uid),
        "unread_list": gym_db.list_unread_member_comments(uid, limit=50),
    })


@router.post("/api/gym/trainers/invite")
def api_gym_trainer_invite(request: Request, body: dict):
    uid, gid = _require_gym_admin(request, int((body or {}).get("gym_id") or 0))
    code = gym_db.create_trainer_invite(gid, uid, days=7)
    return {"code": code, "role": "trainer", "gym_id": gid}


@router.post("/api/gym/trainers/remove")
def api_gym_trainer_remove(request: Request, body: dict):
    uid, gid = _require_gym_admin(request, int((body or {}).get("gym_id") or 0))
    mid = int((body or {}).get("membership_id") or 0)
    if not mid:
        raise HTTPException(status_code=400, detail="membership_id が必要です")
    res = gym_db.remove_staff(mid, by_user_id=uid)
    if not res.get("ok"):
        if res.get("reason") == "has_members":
            raise HTTPException(
                status_code=409,
                detail=f"担当会員が{res.get('count', 0)}名います。先に別のトレーナーへ移管してください")
        raise HTTPException(status_code=400, detail="このスタッフは解除できません")
    return {"ok": True}


@router.post("/api/gym/members/trainer")
def api_gym_member_trainer(request: Request, body: dict):
    uid, gid = _require_gym_admin(request, int((body or {}).get("gym_id") or 0))
    mid = int((body or {}).get("membership_id") or 0)
    if not mid:
        raise HTTPException(status_code=400, detail="membership_id が必要です")
    ok = gym_db.set_member_trainer(mid, (body or {}).get("trainer_id"))
    if not ok:
        raise HTTPException(status_code=404, detail="対象の会員が見つかりません")
    return {"ok": True}


@router.post("/api/gym/members/remove")
def api_gym_member_remove(request: Request, body: dict):
    uid, gid = _require_gym_admin(request, int((body or {}).get("gym_id") or 0))
    mid = int((body or {}).get("membership_id") or 0)
    if not mid:
        raise HTTPException(status_code=400, detail="membership_id が必要です")
    res = gym_db.remove_member(mid, by_user_id=uid)
    if not res.get("ok", True):
        raise HTTPException(status_code=400, detail="解除できませんでした")
    return {"ok": True}


@router.get("/api/gym/my-comments")
def api_gym_my_comments(request: Request, limit: int = 30, offset: int = 0):
    # 自分が担当する会員のコメントのみ返すため、スタッフ（トレーナー/ジム管理者）で可
    uid = _require_staff(request)
    return {
        "comments": gym_db.list_my_member_comments(uid, limit=max(1, min(limit, 100)),
                                                   offset=max(0, offset)),
        "total": gym_db.count_my_member_comments(uid),
    }


# ================= Phase 6: 質問スレッド・未確認・ジム設定 =================

@router.get("/trainer/questions", response_class=HTMLResponse)
def trainer_questions(request: Request):
    """担当会員からの質問に答える（スレッド一覧＋未確認）."""
    uid = _require_staff(request)
    return _render(request, "trainer_questions.html", {
        "request": request, "me": uid,
        "threads": gym_db.list_member_threads(uid),
        "unread": gym_db.count_unread_member_comments(uid),
        "unread_list": gym_db.list_unread_member_comments(uid, limit=50),
    })


@router.get("/api/gym/threads")
def api_gym_threads(request: Request):
    uid = _require_staff(request)
    return {
        "threads": gym_db.list_member_threads(uid),
        "unread": gym_db.count_unread_member_comments(uid),
        "comments": gym_db.list_unread_member_comments(uid, limit=50),
    }


@router.get("/api/gym/thread/{member_id}")
def api_gym_thread(request: Request, member_id: str):
    uid = _require_staff(request)
    return {
        "member_id": member_id,
        "comments": gym_db.fetch_member_thread(uid, member_id),
        "unread": gym_db.count_unread_member_comments(uid),
    }


@router.post("/api/gym/thread/reply")
def api_gym_thread_reply(request: Request, body: dict):
    uid = _require_staff(request)
    b = body or {}
    member_id = str(b.get("member_id") or "").strip()
    text = str(b.get("body") or "").strip()
    if not member_id or not text:
        raise HTTPException(status_code=400, detail="member_id と body が必要です")
    if not gym_db.can_staff_view_member(uid, member_id):
        raise HTTPException(status_code=403, detail="この会員を表示する権限がありません")
    try:
        reply_to = int(b.get("reply_to_id")) if b.get("reply_to_id") else None
    except (TypeError, ValueError):
        reply_to = None
    directive = bool(b.get("is_directive"))
    cid = gym_db.add_reply(
        member_id=member_id, trainer_id=uid, body=text, reply_to_id=reply_to,
        target_date=today_jst_date().isoformat(), is_directive=directive)
    staff = gym_db.get_user(uid) or {}
    msg = f"💬 {staff.get('display_name') or 'トレーナー'} から返信が届きました\n\n{text}"
    if directive:
        msg += "\n\n⭐ この内容はAIコーチの今後のアドバイスにも反映されます"
    _push_to_member(member_id, msg)
    return {"ok": True, "comment_id": cid}


@router.post("/api/gym/requests/approve")
def api_gym_request_approve(request: Request, body: dict):
    uid = _require_staff(request)
    mid = int((body or {}).get("membership_id") or 0)
    if not mid:
        raise HTTPException(status_code=400, detail="membership_id が必要です")
    req = gym_db.get_request_for_staff(uid, mid)
    if not req:
        raise HTTPException(status_code=404,
                            detail="申請が見つかりません（すでに処理済みの可能性があります）")
    trainer_id = str((body or {}).get("trainer_id") or uid)
    if not gym_db.approve_request(mid, trainer_id=trainer_id):
        raise HTTPException(status_code=409, detail="すでに処理済みです")
    _push_to_member(
        req["user_id"],
        f"🎉 「{req['gym_name']}」への登録が完了しました！\n"
        "これから食事・運動の記録をよろしくお願いします。")
    return {"ok": True}


@router.post("/api/gym/requests/reject")
def api_gym_request_reject(request: Request, body: dict):
    uid = _require_staff(request)
    mid = int((body or {}).get("membership_id") or 0)
    if not mid:
        raise HTTPException(status_code=400, detail="membership_id が必要です")
    if not gym_db.get_request_for_staff(uid, mid):
        raise HTTPException(status_code=404,
                            detail="申請が見つかりません（すでに処理済みの可能性があります）")
    if not gym_db.reject_request(mid):
        raise HTTPException(status_code=409, detail="すでに処理済みです")
    return {"ok": True}


@router.post("/api/gym/settings")
def api_gym_settings(request: Request, body: dict):
    """ジム名の変更・入会コードの再発行（ジム管理者のみ）."""
    uid, gid = _require_gym_admin(request, int((body or {}).get("gym_id") or 0))
    b = body or {}
    out = {}
    name = str(b.get("name") or "").strip()
    if name:
        if not gym_db.rename_gym(gid, name):
            raise HTTPException(status_code=400, detail="ジム名を変更できませんでした")
        out["name"] = name
    if b.get("regenerate_code"):
        code = gym_db.regenerate_join_code(gid)
        if not code:
            raise HTTPException(status_code=400, detail="入会コードを再発行できませんでした")
        out["join_code"] = code
    if not out:
        raise HTTPException(status_code=400, detail="変更内容がありません")
    return {"ok": True, **out}


@router.get("/api/gym/qr")
def api_gym_qr(request: Request, gym_id: int = 0):
    """入会コードのQR画像（PNG）を返す。会員はLINEで読み取って入会申請できる."""
    uid, gid = _require_gym_admin(request, gym_id)
    import io
    import qrcode
    gym = gym_db.get_gym(gid) or {}
    url = f"{settings.BASE_URL.rstrip('/')}/join?code={gym.get('join_code') or ''}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


# ================= Phase 8: 指導方針（AI反映）の管理 =================

@router.get("/api/gym/directives/{member_id}")
def api_gym_directives(request: Request, member_id: str):
    """その会員について AIコーチに反映中の⭐指導方針."""
    uid = _require_staff(request)
    if not gym_db.can_staff_view_member(uid, member_id):
        raise HTTPException(status_code=403, detail="この会員を表示する権限がありません")
    return {"directives": gym_db.list_active_directives(member_id)}


@router.post("/api/gym/directive/toggle")
def api_gym_directive_toggle(request: Request, body: dict):
    """⭐方針フラグの切り替え（解除できるようにする）."""
    uid = _require_staff(request)
    b = body or {}
    try:
        cid = int(b.get("comment_id") or 0)
    except (TypeError, ValueError):
        cid = 0
    if not cid:
        raise HTTPException(status_code=400, detail="comment_id が必要です")
    res = gym_db.set_directive(cid, bool(b.get("on")), uid)
    if not res:
        raise HTTPException(status_code=404, detail="コメントが見つかりません")
    if res.get("error") == "forbidden":
        raise HTTPException(status_code=403,
                            detail="この会員のコメントを変更する権限がありません")
    return res
