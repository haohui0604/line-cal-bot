from linebot.models import TextMessage, ImageMessage
from linebot.exceptions import LineBotApiError
from app.handlers.text_handler import handle_text
from app.handlers.image_handler import handle_image
import logging

logger = logging.getLogger(__name__)


def on_message(event, line_bot_api):
    user_id = event.source.user_id
    rt = event.reply_token
    if isinstance(event.message, TextMessage):
        out = handle_text(user_id, event.message.text)
    elif isinstance(event.message, ImageMessage):
        out = handle_image(user_id, event.message.id, line_bot_api)
    else:
        return
    if out is None:
        return

    try:
        line_bot_api.reply_message(rt, out)
    except LineBotApiError as e:
        # コールドスタート等で reply_token が失効していても push で届ける
        logger.warning("reply failed (%s) → fallback to push", e)
        try:
            line_bot_api.push_message(user_id, out)
        except LineBotApiError:
            logger.exception("push fallback also failed")
