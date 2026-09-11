# app/main.py
from fastapi import FastAPI, Request, Header, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage   # ← 追加
from app.config import settings
from app.services.db import init_db
from app.webhook import on_message
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="line-cal-bot")
line_bot_api = LineBotApi(settings.LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(settings.LINE_CHANNEL_SECRET)


@app.on_event("startup")
def _startup():
    init_db()
    logger.info("DB initialized at %s", settings.DB_PATH)


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


# ★ 変更点：TextMessage と ImageMessage をそれぞれ登録 ★
@handler.add(MessageEvent, message=TextMessage)
def _on_text(event):
    on_message(event, line_bot_api)


@handler.add(MessageEvent, message=ImageMessage)
def _on_image(event):
    on_message(event, line_bot_api)
