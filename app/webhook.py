from urllib.parse import parse_qs
from linebot.models import TextMessage, ImageMessage, TextSendMessage
from app.handlers.text_handler import handle_text
from app.handlers.image_handler import handle_image
import logging

logger = logging.getLogger(__name__)

# postback の data → 既存コマンド文字列への対応表
CMD_MAP = {
    "daily":   "今日のレポート",
    "weekly":  "今週のレポート",
    "monthly": "今月のレポート",
    "history": "履歴",
    "setup":   "初期設定",
    "persona": "人格設定",
    "help":    "ヘルプ",
}


def _reply_or_push(event, line_bot_api, out):
    """reply 失敗時は push_message で追送（コールドスタート対策）。"""
    try:
        line_bot_api.reply_message(event.reply_token, out)
    except Exception:
        logger.exception("reply failed, fallback to push")
        try:
            line_bot_api.push_message(event.source.user_id, out)
        except Exception:
            logger.exception("push also failed")


def on_message(event, line_bot_api):
    user_id = event.source.user_id
    if isinstance(event.message, TextMessage):
        out = handle_text(user_id, event.message.text)
    elif isinstance(event.message, ImageMessage):
        out = handle_image(user_id, event.message.id, line_bot_api)
    else:
        return
    if out is None:
        return
    _reply_or_push(event, line_bot_api, out)


def on_postback(event, line_bot_api):
    user_id = event.source.user_id
    data = parse_qs(event.postback.data or "")
    key = (data.get("cmd") or [""])[0]

    if key == "weight":   # 体重は入力待ちなので案内だけ返す
        out = TextSendMessage(text="体重を送ってください（例：体重 70.5）")
    elif key in CMD_MAP:
        out = handle_text(user_id, CMD_MAP[key])   # 既存ロジックをそのまま再利用
    else:
        out = TextSendMessage(text="このボタンは現在使えません")

    _reply_or_push(event, line_bot_api, out)
