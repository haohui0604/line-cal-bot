"""無料枠のスリープ対策: アプリ自身が定期的に自分の /health を叩く.

Render無料枠は「15分間アクセスが無い」とスリープし、次のリクエストで
約1分かけて起き上がる。外部pingの保険として、起動中は自分の公開URLに
定期的にアクセスしてアイドル判定をリセットする。
"""
import asyncio
import logging
import random

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_task = None


def _sweep_pending() -> None:
    """放置された写真（15分超）を今日の間食として確定する（通知はしない）.

    Render無料枠はプロセスが落ちると消えるため、自己pingで起きている間に
    まとめて確定する（best-effort）。push は送らないので、LINEの月間
    送信上限を消費しない。
    """
    try:
        from app.handlers.text_handler import sweep_expired_pendings
        done = sweep_expired_pendings()
    except Exception:
        logger.warning("pending sweep failed", exc_info=True)
        return
    if done:
        logger.info("pending sweep: %d件を今日の間食として確定しました", len(done))


async def _loop():
    url = settings.BASE_URL.rstrip("/") + "/health"
    interval = max(60, settings.SELF_PING_INTERVAL_SEC)
    # 起動処理とぶつからないよう少し待つ
    await asyncio.sleep(45)
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            try:
                r = await client.get(url)
                logger.info("self-ping %s -> %s", url, r.status_code)
            except Exception:
                logger.warning("self-ping failed: %s", url, exc_info=True)
            # 放置された写真を確定（今日の間食）＋通知
            _sweep_pending()
            # 間隔に揺らぎを持たせ、毎回同じ秒数で叩かないようにする
            await asyncio.sleep(interval + random.randint(0, 60))


def start_keepalive() -> None:
    """起動時に呼ぶ。無効設定なら何もしない。"""
    global _task
    if not settings.SELF_PING_ENABLED:
        logger.info("self-ping: disabled by settings")
        return
    if not settings.BASE_URL.startswith("https"):
        logger.info("self-ping: skipped (BASE_URL is not https: %s)",
                    settings.BASE_URL)
        return
    if _task is not None and not _task.done():
        return
    try:
        _task = asyncio.create_task(_loop())
    except RuntimeError:
        logger.warning("self-ping: no running event loop; skipped")
        return
    logger.info("self-ping: started (%s/health every ~%ss)",
                settings.BASE_URL.rstrip("/"), settings.SELF_PING_INTERVAL_SEC)
