"""リッチメニューを作成して全ユーザーに適用する（1回だけ実行）。"""
from linebot import LineBotApi
from linebot.models import (RichMenu, RichMenuSize, RichMenuArea,
                            RichMenuBounds, PostbackAction)
from app.config import settings

api = LineBotApi(settings.LINE_CHANNEL_ACCESS_TOKEN)


def area(x, y, label, data, display_text=None):
    return RichMenuArea(
        bounds=RichMenuBounds(x=x, y=y, width=1250, height=562),
        action=PostbackAction(label=label, data=data,
                              display_text=display_text or label))


menu = RichMenu(
    size=RichMenuSize(width=2500, height=1686), selected=True,
    name="メインメニュー", chat_bar_text="メニュー",
    areas=[
        area(   0,    0, "今日",     "cmd=daily",   "今日のレポート"),
        area(1250,    0, "今週",     "cmd=weekly",  "今週のレポート"),
        area(   0,  562, "今月",     "cmd=monthly", "今月のレポート"),
        area(1250,  562, "初期設定", "cmd=setup"),
        area(   0, 1124, "人格設定", "cmd=persona"),
        area(1250, 1124, "体重を記録", "cmd=weight"),
    ],
)

rid = api.create_rich_menu(rich_menu=menu)
with open("richmenu.png", "rb") as f:          # 2500×1686 / PNG / 1MB以下
    api.set_rich_menu_image(rid, "image/png", f.read())
api.set_default_rich_menu(rid)
print("done:", rid)
