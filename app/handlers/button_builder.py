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


def slot_qr():
    """写真アップ後「いつの食事か」を選ぶクイックリプライ（11件）.

    LINEのクイックリプライ上限は13件（12件＋キャンセル）。postback data は slot=<offset>:<slot>。
    """
    return qr(
        pb("今日の朝食", "slot=0:breakfast", "今日の朝食"),
        pb("今日の昼食", "slot=0:lunch", "今日の昼食"),
        pb("今日の夕食", "slot=0:dinner", "今日の夕食"),
        pb("今日の間食", "slot=0:snack", "今日の間食"),
        pb("今日の夜食", "slot=0:snack", "今日の夜食"),
        pb("昨日の朝食", "slot=1:breakfast", "昨日の朝食"),
        pb("昨日の昼食", "slot=1:lunch", "昨日の昼食"),
        pb("昨日の夕食", "slot=1:dinner", "昨日の夕食"),
        pb("昨日の間食", "slot=1:snack", "昨日の間食"),
        pb("昨日の夜食", "slot=1:snack", "昨日の夜食"),
        pb("その他", "slot=other", "その他"),
        pb("キャンセル", "slot=cancel", "キャンセル"),
    )
