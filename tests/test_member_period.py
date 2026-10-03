"""マイ記録ページの期間ナビ（＜過去 / 未来＞）と体重数値のテスト."""

import os
import sys
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "period.db")
os.environ["TURSO_DATABASE_URL"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.dates import today_jst_date  # noqa: E402
from app.web import member  # noqa: E402

TPL = Path(__file__).resolve().parents[1] / "app/templates/member_home.html"


# ---- 期間の計算（offset → 日付範囲） ----

def test_window_latest_is_last_14_days():
    s, e = member._window(14, 0)
    assert e == today_jst_date()
    assert (e - s).days == 13


def test_window_offset1_is_strictly_past():
    s0, e0 = member._window(14, 0)
    s1, e1 = member._window(14, 1)
    assert e1 < s0                 # 1ページ前は直近ウィンドウより完全に過去
    assert (s0 - e1).days == 1     # 隙間なく連続
    assert (e1 - s1).days == 13


def test_past_arrow_goes_to_older_dates():
    # ＜（過去）= offset+1、＞（未来）= offset-1 が「過去/未来」と一致する
    _, e_past = member._window(14, 1)
    _, e_future = member._window(14, 0)
    assert e_past < e_future


def test_window_clamped():
    assert member._window(14, -5) == member._window(14, 0)


# ---- 体重の現在値と増減 ----

def test_weight_stats_normal():
    st = member._weight_stats([
        {"date": "2026-09-20", "weight_kg": 70.0},
        {"date": "2026-09-25", "weight_kg": 69.5},
        {"date": "2026-10-03", "weight_kg": 68.2}])
    assert st["current"] == 68.2 and st["base"] == 70.0
    assert st["delta"] == -1.8
    assert st["base_date"] == "2026-09-20" and st["current_date"] == "2026-10-03"


def test_weight_stats_skips_missing_values():
    st = member._weight_stats([
        {"date": "2026-09-20", "weight_kg": None},
        {"date": "2026-09-21", "weight_kg": 70.0},
        {"date": "2026-10-03", "weight_kg": None}])
    assert st["base"] == 70.0 and st["current"] == 70.0 and st["delta"] == 0.0


def test_weight_stats_empty():
    st = member._weight_stats([])
    assert st["current"] is None and st["delta"] is None
    assert st["base_date"] is None


def test_weight_stats_single_point():
    st = member._weight_stats([{"date": "2026-10-03", "weight_kg": 65.0}])
    assert st["current"] == 65.0 and st["delta"] == 0.0


def test_weight_stats_gain_is_positive():
    st = member._weight_stats([{"date": "2026-09-20", "weight_kg": 65.0},
                               {"date": "2026-10-03", "weight_kg": 66.4}])
    assert st["delta"] == 1.4


# ---- 描画された HTML（実出力）でナビを確認 ----

def _req():
    from starlette.requests import Request
    return Request({"type": "http", "method": "GET", "path": "/me",
                    "headers": [], "query_string": b""})


def _render(offset):
    orig = member.settings.LIFF_ID
    member.settings.LIFF_ID = "2000000000-abcdefgh"
    try:
        return member.member_home(request=_req(), offset=offset).body.decode("utf-8")
    finally:
        member.settings.LIFF_ID = orig


def test_rendered_offset0_shows_only_past_arrow():
    html = _render(0)
    assert 'id="pgPast"' in html and 'href="?offset=1"' in html
    assert 'id="pgFuture"' not in html          # 直近表示では未来へ進めない
    assert member._window(14, 0)[0].isoformat() in html


def test_rendered_offset1_moves_to_past_and_shows_both():
    html = _render(1)
    assert 'id="pgPast"' in html and 'href="?offset=2"' in html
    assert 'id="pgFuture"' in html and 'href="?offset=0"' in html
    start, end = member._window(14, 1)
    assert start.isoformat() in html and end.isoformat() in html
    assert end.isoformat() != member._window(14, 0)[1].isoformat()


def test_rendered_range_differs_between_offsets():
    r0 = _render(0)
    r1 = _render(1)
    assert member._window(14, 0)[1].isoformat() in r0
    assert member._window(14, 0)[1].isoformat() not in r1


# ---- 期間が動かなかった根本原因の再発防止（JSがpayloadを送る） ----

def test_api_forwards_payload_to_server():
    src = TPL.read_text(encoding="utf-8")
    assert "async function api(path, body)" in src
    assert "Object.assign({id_token: idToken}, body || {})" in src
    assert 'api("/api/me/summary", {days: 14, offset: _off})' in src
    assert "pgPrev" not in src and "pgNext" not in src   # 旧JSの取り違えを排除
