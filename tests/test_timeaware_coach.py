"""AIコーチの時間帯対応 + トレーナー引用任意化の回帰テスト."""
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "tw.db")
os.environ["TURSO_DATABASE_URL"] = ""
os.environ["GEMINI_API_KEY"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.services.db import init_db
from app.services import coach, dates, db

JST = timezone(timedelta(hours=9))


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_db()


def _freeze(monkeypatch, hour_jst, day="2026-09-30"):
    """JST の hour:00 になるよう UTC 時刻を固定する."""
    base = datetime.fromisoformat(day + "T00:00:00+00:00")
    dt_utc = base + timedelta(hours=hour_jst - 9)
    fake_now = dt_utc.astimezone(dates.JST)
    monkeypatch.setattr(dates, "now_jst", lambda: fake_now)
    monkeypatch.setattr(coach, "now_jst", lambda: fake_now)
    monkeypatch.setattr(dates, "today_jst", lambda: fake_now.strftime("%Y-%m-%d"))
    monkeypatch.setattr(coach, "today_jst",
                        lambda: fake_now.strftime("%Y-%m-%d"))


def _facts(bucket_date="2026-09-30", goal=2000, intake=500):
    return {"date": bucket_date, "intake": intake, "burn": 2400,
            "goal": goal, "protein_g": 30, "fat_g": 20, "carb_g": 50,
            "weight_kg": None, "foods": ["朝食:トースト 300kcal"],
            "past_trainer": [], "hash": "x",
            "time_bucket": "final",
            "remaining_kcal": goal - intake}


# ---- _time_bucket の境界 ----

@pytest.mark.parametrize("hour,expect", [
    (5, "morning"), (8, "morning"), (10, "morning"),
    (11, "noon"), (13, "noon"), (15, "noon"),
    (16, "evening"), (19, "evening"), (21, "evening"),
    (22, "night"), (0, "night"), (4, "night"),
])
def test_time_bucket_boundaries(monkeypatch, hour, expect):
    _freeze(monkeypatch, hour)
    assert coach._time_bucket("2026-09-30") == expect


def test_past_day_is_final(monkeypatch):
    _freeze(monkeypatch, 8)   # JST 朝8時でも、対象が昨日なら final
    assert coach._time_bucket("2026-09-29") == "final"


# ---- プロンプトへの反映 ----

def test_prompt_shows_time_and_remaining(monkeypatch):
    f = _facts()
    f["time_bucket"] = "noon"
    f["remaining_kcal"] = 1500
    p = coach.build_prompt({"bot_name": "アシ", "bot_tone": "", "bot_pronoun": "わたし"}, f)
    assert "途中経過" in p and "昼" in p
    assert "残り摂取枠: 1500kcal" in p
    assert "夕食" in p  # 夕食の提案を促す指示が入っている
    assert "確定した』かのように低摂取を責めてはいけない" in p


def test_prompt_final_summary_for_past(monkeypatch):
    f = _facts(bucket_date="2026-09-29")
    f["time_bucket"] = "final"
    p = coach.build_prompt({"bot_name": "アシ", "bot_tone": "", "bot_pronoun": "わたし"}, f)
    assert "確定" in p and "1日全体の総括" in p
    assert "途中経過" not in p


def test_prompt_trainer_quote_is_optional():
    f = _facts()
    p = coach.build_prompt({"bot_name": "アシ", "bot_tone": "", "bot_pronoun": "わたし"}, f)
    assert "毎回必ず引用する必要はない" in p
    assert "本当に関係する場合に限り" in p or "引用が助言の助けになる場合" in p


# ---- ルールfallbackの時間帯別 ----

def test_rule_based_noon_shows_remaining():
    f = _facts()
    f["time_bucket"] = "noon"; f["remaining_kcal"] = 1500
    out = coach._rule_based(f)
    assert "昼" in out and "あと 1500kcal" in out and "夕食" in out
    assert "超過" not in out  # 途中経過で収支の断定はしない


def test_rule_based_final_is_summary():
    f = _facts(bucket_date="2026-09-29")
    f["time_bucket"] = "final"
    out = coach._rule_based(f)
    assert "摂取" in out and "消費" in out and ("超過" in out or "以内" in out)


# ---- キャッシュキーが時刻帯で分かれる ----

def test_cache_key_differs_by_bucket(monkeypatch):
    """同じ日・同じデータでも、時刻帯が変わると別キャッシュキーになる."""
    monkeypatch.setattr(coach, "fetch_day_summary",
                        lambda u, d: {"intake_kcal": 500, "protein_g": 0,
                                      "fat_g": 0, "carb_g": 0, "salt_g": 0})
    monkeypatch.setattr(coach, "fetch_day_activity_total", lambda u, d: 2400)
    monkeypatch.setattr(coach, "get_goal", lambda u, d: 2000)
    monkeypatch.setattr(coach, "fetch_entries_for_date", lambda u, d: [])
    monkeypatch.setattr(coach, "fetch_latest_weight",
                        lambda u, on_or_before=None: None)
    monkeypatch.setattr(coach, "fetch_past_trainer_comments",
                        lambda u, before_date=None, days=14, limit=5: [])

    _freeze(monkeypatch, 8)   # 朝
    k_morning = coach._build_facts("U1", "2026-09-30")
    assert k_morning["time_bucket"] == "morning"
    h_morning = k_morning["hash"]

    _freeze(monkeypatch, 13)  # 昼
    k_noon = coach._build_facts("U1", "2026-09-30")
    assert k_noon["time_bucket"] == "noon"
    # time_bucket が facts に含まれるためハッシュも変わる（=再生成される）
    assert k_noon["hash"] != h_morning


def test_cache_key_string_contains_bucket(monkeypatch):
    """get_day_comment のキャッシュキーに time_bucket が入る."""
    calls = {}
    monkeypatch.setattr(coach, "_generate", lambda u, f: "テストコメント")
    monkeypatch.setattr(coach, "save_report_comment",
                        lambda cache_key=None, **kw: calls.update(key=cache_key))
    monkeypatch.setattr(coach, "get_report_comment", lambda k: None)
    monkeypatch.setattr(coach, "fetch_day_summary",
                        lambda u, d: {"intake_kcal": 500, "protein_g": 0,
                                      "fat_g": 0, "carb_g": 0, "salt_g": 0})
    monkeypatch.setattr(coach, "fetch_day_activity_total", lambda u, d: 2400)
    monkeypatch.setattr(coach, "get_goal", lambda u, d: 2000)
    monkeypatch.setattr(coach, "fetch_entries_for_date", lambda u, d: [])
    monkeypatch.setattr(coach, "fetch_latest_weight",
                        lambda u, on_or_before=None: None)
    monkeypatch.setattr(coach, "fetch_past_trainer_comments",
                        lambda u, before_date=None, days=14, limit=5: [])
    monkeypatch.setattr(coach, "load_user_persona",
                        lambda u: {"bot_name": "アシ", "bot_tone": "", "bot_pronoun": "わたし"})
    monkeypatch.setattr(coach, "fetch_active_directives", lambda u: [])
    monkeypatch.setattr(coach, "goal_context_line", lambda u: "")
    _freeze(monkeypatch, 13)  # 昼
    coach.get_day_comment("U1", "2026-09-30")
    assert ":noon:" in calls["key"]
