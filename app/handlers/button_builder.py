"""クイックリプライの組み立て部品。"""
from linebot.models import (
    QuickReply, QuickReplyButton, PostbackAction, MessageAction,
)


def qr(*buttons):
    return QuickReply(items=list(buttons))


def pb(label, data, display_text=None):
    """postback型ボタン（トークには display_text が表示される）。"""
    return QuickReplyButton(
        action=PostbackAction(label=label, data=data,
                              display_text=display_text or label)
    )


def msq(label, text=None):
    """メッセージ型ボタン（タップすると text がユーザ発言として送信される）。"""
    return QuickReplyButton(
        action=MessageAction(label=label, text=text or label)
    )
