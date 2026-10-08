"""システム管理者画面 (Phase 4).

- GET  /system                        … ダッシュボード（システム管理者のみ）
- POST /api/system/gym/create         … ジム作成
- POST /api/system/gym/delete         … ジム論理削除（担当トレーナーは空欄に）
- POST /api/system/gym/admin/invite   … ジム管理者の招待コード発行
- POST /api/system/gym/admin/remove   … ジム管理者の解除
- POST /api/system/system-admin/invite / remove … システム管理者の増減
- GET  /api/system/gym/{id}/staff     … 当該ジムのスタッフ一覧
- GET  /api/system/analytics          … 運営KPI

認証は「環境変数 ADMIN_USER_IDS」または「system_admins テーブル」。
"""
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import auth
from app.config import settings
from app.services import admin_db
from app.services import line_insight

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _ensure_tables() -> None:
    """管理画面のテーブルが無い環境でも動くように、アクセス時に冪等作成する."""
    try:
        from app.services.db import get_conn
        with get_conn() as c:
            admin_db.ensure_admin_tables(c)
    except Exception:
        logger.warning("管理テーブルの準備に失敗", exc_info=True)


def _require_system_admin(request: Request) -> str:
    _ensure_tables()
    uid = auth.current_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="ログインが必要です")
    if not admin_db.is_system_admin(uid):
        raise HTTPException(status_code=403, detail="システム管理者のみアクセスできます")
    return uid


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


@router.get("/system", response_class=HTMLResponse)
def system_home(request: Request):
    uid = auth.current_user_id(request)
    if not uid or not admin_db.is_system_admin(uid):
        return RedirectResponse("/login")
    _ensure_tables()
    ctx = {
        "request": request,
        "me": uid,
        "gyms": admin_db.list_gyms(),
        "active_gyms": admin_db.count_active_gyms(),
        "admins": admin_db.list_system_admins(),
        "env_admins": sorted(settings.admin_user_id_set),
        "audit": admin_db.list_audit(30),
        "analytics": admin_db.analytics(),
        "insight": line_insight.get_follower_stats(),
    }
    try:  # Starlette 0.29+ は (request, name, context)
        return templates.TemplateResponse(
            request=request, name="system_admin.html", context=ctx)
    except TypeError:  # 旧シグネチャ
        return templates.TemplateResponse("system_admin.html", ctx)


@router.get("/api/system/analytics")
def api_analytics(request: Request):
    _require_system_admin(request)
    data = admin_db.analytics()
    data["insight"] = line_insight.get_follower_stats()
    return data


@router.post("/api/system/gym/create")
def api_gym_create(request: Request, body: dict):
    uid = _require_system_admin(request)
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="ジム名を入力してください")
    try:
        g = admin_db.create_gym(name,
                                owner_user_id=(body.get("owner_user_id") or uid))
    except Exception as e:            # 原因を画面に出す（握りつぶさない）
        logger.exception("gym create failed")
        raise HTTPException(status_code=500, detail=f"ジム作成に失敗: {e}")
    admin_db.add_audit(uid, "gym.create", "gym", str(g["id"]), name)
    return g


@router.post("/api/system/gym/delete")
def api_gym_delete(request: Request, body: dict):
    uid = _require_system_admin(request)
    gid = int(body.get("gym_id") or 0)
    if not gid:
        raise HTTPException(status_code=400, detail="gym_id が必要です")
    reason = body.get("reason") or ""
    ok = admin_db.soft_delete_gym(gid, uid, reason)
    admin_db.add_audit(uid, "gym.delete", "gym", str(gid), reason)
    return {"ok": ok}


@router.post("/api/system/gym/restore")
def api_gym_restore(request: Request, body: dict):
    uid = _require_system_admin(request)
    gid = int(body.get("gym_id") or 0)
    if not gid:
        raise HTTPException(status_code=400, detail="gym_id が必要です")
    ok = admin_db.restore_gym(gid, uid)
    admin_db.add_audit(uid, "gym.restore", "gym", str(gid))
    return {"ok": ok}


@router.post("/api/system/gym/admin/invite")
def api_gym_admin_invite(request: Request, body: dict):
    uid = _require_system_admin(request)
    gid = int(body.get("gym_id") or 0)
    if not gid:
        raise HTTPException(status_code=400, detail="gym_id が必要です")
    code = admin_db.create_invite(gid, uid, role="gym_admin")
    admin_db.add_audit(uid, "gym.admin.invite", "gym", str(gid), code)
    return {"code": code, "role": "gym_admin"}


@router.post("/api/system/gym/admin/remove")
def api_gym_admin_remove(request: Request, body: dict):
    uid = _require_system_admin(request)
    mid = int(body.get("membership_id") or 0)
    if not mid:
        raise HTTPException(status_code=400, detail="membership_id が必要です")
    ok = admin_db.remove_membership(mid, uid, body.get("reason") or "")
    admin_db.add_audit(uid, "gym.admin.remove", "membership", str(mid))
    return {"ok": ok}


@router.post("/api/system/system-admin/invite")
def api_system_admin_invite(request: Request, body: dict):
    uid = _require_system_admin(request)
    target = (body.get("user_id") or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="user_id が必要です")
    admin_db.add_system_admin(target, body.get("note") or "", created_by=uid)
    admin_db.add_audit(uid, "system_admin.add", "user", target)
    return {"ok": True, "user_id": target}


@router.post("/api/system/system-admin/remove")
def api_system_admin_remove(request: Request, body: dict):
    uid = _require_system_admin(request)
    target = (body.get("user_id") or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="user_id が必要です")
    if not admin_db.remove_system_admin(target):
        raise HTTPException(status_code=400,
                            detail="最後のシステム管理者は削除できません")
    admin_db.add_audit(uid, "system_admin.remove", "user", target)
    return {"ok": True}


@router.get("/api/system/gym/{gym_id}/staff")
def api_gym_staff(request: Request, gym_id: int):
    _require_system_admin(request)
    return {"staff": admin_db.list_gym_staff(gym_id)}
