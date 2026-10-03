from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, PostbackEvent
from linebot.models import TextSendMessage
from app.config import settings
from app.services.db import init_db
from app.services.keepalive import start_keepalive
from app.webhook import on_message, on_postback
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="line-cal-bot")

# 静的ファイル（日別ビューの日付演算など）
import os as _os
_STATIC = _os.path.join(_os.path.dirname(__file__), "static")
if _os.path.isdir(_STATIC):
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")

# Web (LINEログイン / LIFF) ルート
from app.web.routes import router as web_router
app.include_router(web_router)

# トレーナー / ジム管理者向け画面 (Phase 2)
from app.web.staff import router as staff_router
app.include_router(staff_router)

# 会員向けLIFF画面 (Phase 3)
from app.web.member import router as member_router
app.include_router(member_router)

# 日別ビュー (Phase 3.5: 会員/トレーナー共用)
from app.web.day import router as day_router
app.include_router(day_router)

# システム管理者画面 (Phase 4)
from app.web.admin import router as admin_router
app.include_router(admin_router)

line_bot_api = LineBotApi(settings.LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(settings.LINE_CHANNEL_SECRET)


@app.on_event("startup")
async def _startup():
    init_db()
    logger.info("DB initialized at %s", settings.DB_PATH)
    # ADMIN_USER_IDS を system_admins テーブルへ取り込む（冪等）
    from app.services import admin_db
    admin_db.bootstrap_env_admins()
    # 無料枠のスリープ対策（BASE_URL が https のときだけ動く）
    start_keepalive()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/callback")
async def callback(
    request: Request,
    x_line_signature: str = Header(alias="X-Line-Signature"),
):
    body = (await request.body()).decode("utf-8")
    try:
        handler.handle(body, x_line_signature)
    except InvalidSignatureError:
        logger.warning("invalid signature")
        raise HTTPException(status_code=400, detail="invalid signature")
    return {"ok": True}


# テキストメッセージ
@handler.add(MessageEvent, message=TextMessage)
def _on_text(event):
    on_message(event, line_bot_api)


# 画像メッセージ（← 前回の修正。これが無いと画像が無言になる）
@handler.add(MessageEvent, message=ImageMessage)
def _on_image(event):
    on_message(event, line_bot_api)


# リッチメニュー等のpostback → コマンドに変換して既存ロジックへ流す
@handler.add(PostbackEvent)
def _on_postback(event):
    try:
        on_postback(event, line_bot_api)
    except Exception:
        logger.exception("postback handling failed")
