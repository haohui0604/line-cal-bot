"""LINE公式アカウントの友だち数・ブロック数を取得する（Messaging API Insight）.

友だち数 = ボットを友だち追加している人の数（ブロックした人も含む）
ブロック数 = そのうちボットをブロックしている人の数
到達数 = ブロックや未受信を除いて実際に配信が届く人の数
データは前日分まで（JST）。取得失敗時は error を入れて返し、例外は投げない。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

INSIGHT_URL = "https://api.line.me/v2/bot/insight/followers"
JST = timezone(timedelta(hours=9))


def _token() -> str:
    try:
        from app.config import settings
        for attr in ("channel_access_token", "LINE_CHANNEL_ACCESS_TOKEN"):
            v = getattr(settings, attr, None)
            if v:
                return str(v)
    except Exception:
        pass
    return os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "") or ""


def default_date() -> str:
    """Insight は前日までのデータなので JST の前日を yyyyMMdd で返す."""
    return (datetime.now(JST) - timedelta(days=1)).strftime("%Y%m%d")


def get_follower_stats(date: Optional[str] = None, timeout: float = 6.0) -> Dict[str, Any]:
    d = date or default_date()
    token = _token()
    if not token:
        return {"ok": False, "date": d, "error": "チャネルアクセストークン未設定"}
    try:
        r = httpx.get(INSIGHT_URL, params={"date": d},
                      headers={"Authorization": "Bearer " + token}, timeout=timeout)
    except Exception as e:  # ネットワーク不通など
        logger.warning("insight request failed: %s", e)
        return {"ok": False, "date": d, "error": "接続エラー"}
    if r.status_code != 200:
        logger.warning("insight status=%s body=%s", r.status_code, r.text[:200])
        return {"ok": False, "date": d, "error": "HTTP %s" % r.status_code}
    try:
        j = r.json()
    except Exception:
        return {"ok": False, "date": d, "error": "JSON解析失敗"}
    status = j.get("status")
    if status != "ready":
        return {"ok": False, "date": d, "status": status,
                "error": "データ未準備（%s）" % status}
    return {"ok": True, "date": d, "status": status,
            "followers": j.get("followers"),
            "targetedReaches": j.get("targetedReaches"),
            "blocks": j.get("blocks")}
