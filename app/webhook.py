import logging
import re
from urllib.parse import parse_qs
from linebot.models import TextMessage, ImageMessage, TextSendMessage
from app.handlers.text_handler import handle_text
from app.handlers.image_handler import handle_image
from app.config import settings
from app.services.gym_db import (
    upsert_user, find_gym_by_code, request_join, create_gym,
)

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

# 確認ボタンの postback → handle_text 内部トークンへの対応表
TOKEN_MAP = {
    "confirm_yes": "confirm:yes",   # AI推定の記録を確定
    "confirm_no":  "confirm:no",    # AI推定の記録をキャンセル
    "delete_yes":  "delete:yes",    # 削除を確定
    "delete_no":   "delete:no",     # 削除をキャンセル
}

# 入会コード（例: "GYM-5821" / "入会 GYM-5821"）
JOIN_CODE_PAT = re.compile(r"^\s*(?:入会[：:\s]*)?(GYM-[0-9A-Za-z]{4,8})\s*$")

# ユーザーID確認（例: "ID"）
ID_PAT = re.compile(r"^\s*(ID|id|ＩＤ|ユーザーID)\s*$")

# ジム作成（例: "ジム作成 ボディメイクジム渋谷"）
CREATE_GYM_PAT = re.compile(r"^\s*ジム作成[：:\s]+(\S.{0,30}?)\s*$")


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


def _handle_join_code(user_id: str, code: str, line_bot_api):
    """ジムコードによる入会申請（承認制）。既存の記録機能とは独立して動く。"""
    # users テーブルに存在を保証（プロフィール取得は失敗しても続行）
    display_name, picture_url = None, None
    try:
        prof = line_bot_api.get_profile(user_id)
        display_name = prof.display_name
        picture_url = str(prof.picture_url) if prof.picture_url else None
    except Exception:
        logger.exception("get_profile failed (join code flow)")
    upsert_user(user_id, display_name, picture_url)

    gym = find_gym_by_code(code)
    if not gym:
        return TextSendMessage(
            text="入会コードが見つかりませんでした。\n"
                 "ジムから案内されたコードをご確認ください（例: GYM-5821）")
    res = request_join(user_id=user_id, gym_id=gym["id"])
    if res["result"] == "already_active":
        return TextSendMessage(
            text=f"すでに「{gym['name']}」に登録済みです。\n"
                 "記録はいつも通り送ってください。")
    if res["result"] == "already_pending":
        return TextSendMessage(
            text=f"「{gym['name']}」への登録申請は受付済みです。\n"
                 "トレーナーの承認をお待ちください。")
    return TextSendMessage(
        text=f"「{gym['name']}」への登録申請を受け付けました！\n"
             "トレーナーの承認後に登録が完了します。")


def _get_profile_safe(user_id, line_bot_api):
    """プロフィール取得（失敗しても (None, None) で続行）。"""
    try:
        prof = line_bot_api.get_profile(user_id)
        pic = str(prof.picture_url) if prof.picture_url else None
        return prof.display_name, pic
    except Exception:
        logger.exception("get_profile failed")
        return None, None


def _handle_create_gym(user_id: str, name: str, line_bot_api):
    """管理者専用: LINEからジムを作成し、発行者をオーナー(gym_admin)にする。

    ADMIN_USER_IDS に含まれないユーザからの実行は拒否する。
    """
    if user_id not in settings.admin_user_id_set:
        logger.warning("unauthorized gym creation attempt by %s", user_id)
        return TextSendMessage(
            text="ジムの作成は管理者のみ行えます。")
    display_name, picture_url = _get_profile_safe(user_id, line_bot_api)
    upsert_user(user_id, display_name, picture_url)
    gym = create_gym(name=name, owner_user_id=user_id)
    return TextSendMessage(
        text=f"ジム「{gym['name']}」を作成しました！🏋️\n\n"
             f"入会コード: {gym['join_code']}\n\n"
             "会員さんにこのコードを伝えて、\n"
             "このボットに送信してもらってください。")


def on_message(event, line_bot_api):
    user_id = event.source.user_id
    if isinstance(event.message, TextMessage):
        text = event.message.text
        if ID_PAT.match(text):
            out = TextSendMessage(
                text=f"あなたのユーザーID:\n{user_id}")
        else:
            mg = CREATE_GYM_PAT.match(text)
            m = JOIN_CODE_PAT.match(text)
            if mg:
                out = _handle_create_gym(user_id, mg.group(1), line_bot_api)
            elif m:
                out = _handle_join_code(user_id, m.group(1).upper(), line_bot_api)
            else:
                out = handle_text(user_id, text)
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
    elif key in TOKEN_MAP:   # 確認ボタン → 内部トークンに変換して既存ロジックへ
        out = handle_text(user_id, TOKEN_MAP[key])
    elif key in CMD_MAP:
        out = handle_text(user_id, CMD_MAP[key])   # 既存ロジックをそのまま再利用
    else:
        out = TextSendMessage(text="このボタンは現在使えません")

    _reply_or_push(event, line_bot_api, out)
