"""クイックリプライの組み立て部品。"""
from linebot.models import QuickReply, QuickReplyButton, PostbackAction


def qr(*buttons):
    return QuickReply(items=list(buttons))


def pb(label, data, display_text=None):
    return QuickReplyButton(
        action=PostbackAction(label=label, data=data,
                              display_text=display_text or label)
    )
