"""リッチメニューを作成して全ユーザーに適用する（1回だけ実行）.

使い方:
    python scripts/setup_rich_menu.py

前提:
- 環境変数 LINE_CHANNEL_ACCESS_TOKEN が設定済み
- 環境変数 LIFF_ID が設定済み（マイページボタンに使用）
- 2500x1686 のPNG画像 assets/richmenu.png（このリポジトリに同梱済み）
  （2x2の4分割。上から: マイページ / 初期設定 / AI人格設定 / 使い方）
"""
import os
from linebot import LineBotApi
from linebot.models import (RichMenu, RichMenuSize, RichMenuArea,
                            RichMenuBounds, PostbackAction, URIAction)
from app.config import settings

api = LineBotApi(settings.LINE_CHANNEL_ACCESS_TOKEN)

if not settings.LIFF_ID:
    raise SystemExit("環境変数 LIFF_ID が未設定です")

liff_url = f"https://liff.line.me/{settings.LIFF_ID}"


def pb_area(x, y, label, data, display_text=None):
    return RichMenuArea(
        bounds=RichMenuBounds(x=x, y=y, width=1250, height=843),
        action=PostbackAction(label=label, data=data,
                              display_text=display_text or label))


menu = RichMenu(
    size=RichMenuSize(width=2500, height=1686), selected=True,
    name="メインメニュー", chat_bar_text="メニュー",
    areas=[
        # 左上: マイページ（LIFF=Web）
        RichMenuArea(
            bounds=RichMenuBounds(x=0, y=0, width=1250, height=843),
            action=URIAction(label="マイページ", uri=liff_url)),
        # 右上: 初期設定（目的ウィザード: 減量/減塩/筋肉をボタンで選択）
        pb_area(1250, 0, "初期設定", "cmd=goal", "初期設定"),
        # 左下: AI人格設定（既存コマンド）
        pb_area(0, 843, "AI人格設定", "cmd=persona", "人格設定"),
        # 右下: 使い方（既存ヘルプ）
        pb_area(1250, 843, "使い方", "cmd=help", "使い方"),
    ],
)

# ---- 既存リッチメニューの整理 ----
# OAM(GUI)では API 作成のメニューを編集/削除できないため、
# 既存メニューはここで全て解除・削除してから新しいものを既定にする。
try:
    api.cancel_default_rich_menu()   # 既定解除（無ければ何も起きない）
except Exception:
    pass

for rm in api.get_rich_menu_list():
    try:
        api.delete_rich_menu(rm.rich_menu_id)
        print("deleted old menu:", rm.rich_menu_id, getattr(rm, "name", ""))
    except Exception as e:
        print("delete failed:", rm.rich_menu_id, e)

rid = api.create_rich_menu(rich_menu=menu)
_img_path, _img_type = ("assets/richmenu.jpg", "image/jpeg") \
    if __import__("os").path.exists("assets/richmenu.jpg") \
    else ("assets/richmenu.png", "image/png")
print("using image:", _img_path, _img_type)
with open(_img_path, "rb") as f:          # 2500x1686 / PNG / 1MB以下
    api.set_rich_menu_image(rid, _img_type, f.read())
api.set_default_rich_menu(rid)
print("done:", rid)
print("マイページURL:", liff_url)
