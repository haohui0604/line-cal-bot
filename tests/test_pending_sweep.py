"""放置写真の自動確定（スイープ）が push を送らないことの確認."""

import sys
import time

from app.handlers import text_handler as th
from app.services import keepalive


def _seed(uid, **kw):
    th._pending.clear()
    p = {"foods": [{"name": "x", "kcal": 10}], "meal_slot": "snack",
         "date": "2026-10-02", "awaiting_slot": True, "created_at": time.time()}
    p.update(kw)
    th._pending[uid] = p
    return p


def test_sweep_registers_expired_without_push(monkeypatch):
    saved = {}
    monkeypatch.setattr(th, "_save_foods",
                        lambda uid, foods, slot, d=None: saved.update(
                            slot=slot, d=d, uid=uid))
    _seed("s1", created_at=time.time() - 1000)
    # linebot を読み込めない状態にしても動く（=push を送っていない）ことの確認
    monkeypatch.setitem(sys.modules, "linebot", None)
    keepalive._sweep_pending()
    assert saved["slot"] == "snack" and saved["d"] == "2026-10-02"
    assert "s1" not in th._pending


def test_sweep_keeps_recent_pending(monkeypatch):
    monkeypatch.setattr(th, "_save_foods", lambda *a, **k: None)
    _seed("s2")
    keepalive._sweep_pending()
    assert "s2" in th._pending


def test_sweep_skips_other_wait(monkeypatch):
    monkeypatch.setattr(th, "_save_foods", lambda *a, **k: None)
    _seed("s3", awaiting_other=True, created_at=time.time() - 1000)
    keepalive._sweep_pending()
    assert "s3" in th._pending


def test_sweep_no_pending_is_noop(monkeypatch):
    th._pending.clear()
    keepalive._sweep_pending()   # 例外が出なければOK
    assert th._pending == {}
