from fastapi import FastAPI, Request, Header, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, PostbackEvent
from linebot.models import TextSendMessage
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


# テキストメッセージ
@handler.add(MessageEvent, message=TextMessage)
def _on_text(event):
    on_message(event, line_bot_api)


# 画像メッセージ（← 前回の修正。これが無いと画像が無言になる）
@handler.add(MessageEvent, message=ImageMessage)
def _on_image(event):
    on_message(event, line_bot_api)


# リッチメニュー等のpostback（退会など）→ 落ちずにテキスト返信
@handler.add(PostbackEvent)
def _on_postback(event):
    try:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=(
                "ボタン操作を受け付けました。\n"
                "メニューの操作は文字入力でもできます：\n"
                "・集計 ・履歴 ・週次 ・月次 ・体重 ・初期設定"
            )),
        )
    except Exception:
        logger.exception("postback reply failed")
