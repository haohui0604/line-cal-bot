"""日別ビューの初期表示日＝前日(JST) の回帰テスト.

注意すべき境界:
  - サーバーは UTC で動く。JST の深夜0:00〜8:59 は UTC ではまだ前日。
    UTC の date.today() を使うと「前日」がさらに1日ずれる。
  - ?date= の明示指定は初期値より優先されること。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import dates                      # noqa: E402
from app.services import gym_db                     # noqa: E402
from app.services.db import init_db, fetch_entries_for_date  # noqa: E402
from app.web import day as day_mod                  # noqa: E402
from app import auth                                # noqa: E402

JST = timezone(timedelta(hours=9))
MEMBER = "Udefault_date_test"


def _freeze_utc(monkeypatch, iso_utc: str) -> datetime:
    """UTC時刻を固定し、JSTに変換した datetime を返す."""
    dt = datetime.fromisoformat(iso_utc).astimezone(JST)
    monkeypatch.setattr(dates, "now_jst", lambda: dt)
    return dt


class _FakeReq:
    cookies = {}
    headers = {}


def _capture_template_ctx(monkeypatch):
    captured = {}

    def fake(name, ctx):
        captured["name"] = name
        captured.update(ctx)
        return ctx

    monkeypatch.setattr(day_mod.templates, "TemplateResponse", fake)
    return captured


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(day_mod.router)
    init_db()
    gym_db.upsert_user(MEMBER, "テスト会員", None)
    return TestClient(app)


def _as_member(monkeypatch):
    monkeypatch.setattr(
        auth, "verify_id_token",
        lambda tok: {"sub": MEMBER, "name": "テスト会員", "picture": None})


# --- 1. JST 深夜の境界 ---

def test_yesterday_jst_at_jst_midnight(monkeypatch):
    """UTC では前日でも、JST では日付が変わっているケース."""
    # 2026-09-29 16:00 UTC = 2026-09-30 01:00 JST
    _freeze_utc(monkeypatch, "2026-09-29T16:00:00+00:00")
    assert dates.today_jst() == "2026-09-30"
    assert dates.yesterday_jst() == "2026-09-29"
    # UTC の date.today() を使うと 1 日ずれることを明示的に確認
    assert datetime.fromisoformat("2026-09-29T16:00:00+00:00").date().isoformat() \
        == "2026-09-29"  # UTC基準の「今日」
    assert dates.yesterday_jst() != "2026-09-28"


def test_yesterday_jst_just_before_midnight(monkeypatch):
    """JST 23:59 のケース."""
    _freeze_utc(monkeypatch, "2026-09-29T14:59:00+00:00")  # JST 23:59
    assert dates.today_jst() == "2026-09-29"
    assert dates.yesterday_jst() == "2026-09-28"


def test_yesterday_jst_crosses_month_boundary(monkeypatch):
    _freeze_utc(monkeypatch, "2026-09-30T16:00:00+00:00")  # JST 2026-10-01 01:00
    assert dates.yesterday_jst() == "2026-09-30"


# --- 2. API の初期値 ---

def test_api_defaults_to_yesterday(client, monkeypatch):
    _freeze_utc(monkeypatch, "2026-09-29T16:00:00+00:00")  # JST 2026-09-30 01:00
    _as_member(monkeypatch)
    r = client.post("/api/day/data", json={"id_token": "dummy"})
    assert r.status_code == 200, r.text
    assert r.json()["date"] == "2026-09-29"


def test_api_explicit_date_overrides_default(client, monkeypatch):
    _freeze_utc(monkeypatch, "2026-09-29T16:00:00+00:00")
    _as_member(monkeypatch)
    r = client.post("/api/day/data",
                    json={"id_token": "dummy", "date": "2026-08-15"})
    assert r.status_code == 200, r.text
    assert r.json()["date"] == "2026-08-15"


def test_api_null_date_uses_yesterday(client, monkeypatch):
    """date:null でも 422 にならず前日を返す（過去の422バグの再発防止）."""
    _freeze_utc(monkeypatch, "2026-09-29T16:00:00+00:00")
    _as_member(monkeypatch)
    r = client.post("/api/day/data", json={"id_token": "dummy", "date": None})
    assert r.status_code == 200, r.text
    assert r.json()["date"] == "2026-09-29"


# --- 3. ページの初期表示 ---

def test_page_initial_date_is_yesterday(monkeypatch):
    _freeze_utc(monkeypatch, "2026-09-29T16:00:00+00:00")
    captured = _capture_template_ctx(monkeypatch)
    day_mod.day_page(_FakeReq(), member_id="", date="")
    assert captured["initial_date"] == "2026-09-29"


def test_page_explicit_date_overrides(monkeypatch):
    _freeze_utc(monkeypatch, "2026-09-29T16:00:00+00:00")
    captured = _capture_template_ctx(monkeypatch)
    day_mod.day_page(_FakeReq(), member_id="", date="2026-08-15")
    assert captured["initial_date"] == "2026-08-15"


def test_member_day_page_initial_date_is_yesterday(monkeypatch):
    """会員LIFFの日別ビューも前日が初期表示."""
    from app.web import member as member_mod
    _freeze_utc(monkeypatch, "2026-09-29T16:00:00+00:00")
    captured = _capture_template_ctx_member(monkeypatch, member_mod)
    member_mod.member_day_page()
    assert captured["initial_date"] == "2026-09-29"


def _capture_template_ctx_member(monkeypatch, mod):
    captured = {}

    def fake(name, ctx):
        captured["name"] = name
        captured.update(ctx)
        return ctx

    monkeypatch.setattr(mod.templates, "TemplateResponse", fake)
    return captured


# --- 4. フロント側のフォールバック ---

def test_template_fallback_uses_yesterday():
    html = (ROOT / "app" / "templates" / "day_detail.html").read_text(
        encoding="utf-8")
    assert "DayNav.yesterdayLocal()" in html
    js = (ROOT / "app" / "static" / "js" / "day_nav.js").read_text(
        encoding="utf-8")
    assert "yesterdayLocal" in js
