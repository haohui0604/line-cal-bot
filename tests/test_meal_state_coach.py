"""AIコメントの「状況（時刻帯 × 登録済みの食事区分）」判定の回帰テスト.

検証すること:
  1. 時刻帯 × 食事の登録有無 → scenario が1つに確定する
  2. 夕食を登録済みのときは「夕食にあと◯◯kcal」を絶対に出さない
  3. デザート・間食の許容量は「目標に余力がある」ときだけ出す
  4. 昼前で朝食なし／昼過ぎで昼食なし は「抜いたのか・登録がまだか」を尋ねる
  5. キャッシュキーに時刻帯と scenario が入る（登録・時間で作り直される）
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "meals.db")
os.environ["TURSO_DATABASE_URL"] = ""
os.environ["GEMINI_API_KEY"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import coach  # noqa: E402


class _T:
    def __init__(self, hour):
        self.hour = hour


def freeze(monkeypatch, hour, date="2026-09-30"):
    monkeypatch.setattr(coach, "today_jst", lambda: date)
    monkeypatch.setattr(coach, "now_jst", lambda: _T(hour))


def F(slots, *, intake=1200, burn=2000, goal=1800, date="2026-09-30"):
    f = {"date": date, "intake": intake, "burn": burn, "goal": goal,
         "protein_g": 60.0, "fat_g": 40.0, "carb_g": 130.0, "weight_kg": None,
         "foods": [], "past_trainer": [], "hash": "x", "slot_kcal": slots}
    f["time_bucket"] = coach._time_bucket(date)
    f["remaining_kcal"] = goal - intake
    f["balance_kcal"] = intake - burn
    f["meal_state"] = coach._meal_state(f)
    return f


# ---- 1. 状態表 ----

CASES = [
    (7, {}, "morning_empty"),
    (7, {"breakfast": 400}, "morning_logged"),
    (9, {"snack": 200}, "morning_partial"),
    (12, {}, "midday_empty"),
    (12, {"breakfast": 400}, "lunch_missing"),
    (14, {"breakfast": 400, "lunch": 600}, "lunch_logged"),
    (18, {"breakfast": 400, "lunch": 600}, "dinner_budget"),
    (18, {"breakfast": 400, "lunch": 600, "dinner": 700}, "dinner_logged"),
    (18, {}, "evening_empty"),
    (22, {"breakfast": 400, "lunch": 600, "dinner": 700},
     "night_dinner_logged"),
    (22, {"lunch": 600}, "night_no_dinner"),
    (22, {}, "night_empty"),
]


def test_scenario_table(monkeypatch):
    for hour, slots, expected in CASES:
        freeze(monkeypatch, hour)
        f = F(slots)
        assert f["meal_state"]["scenario"] == expected, (hour, slots)


def test_past_date_is_final(monkeypatch):
    freeze(monkeypatch, 3)
    f = F({"breakfast": 400}, date="2026-09-29")
    assert f["meal_state"]["scenario"] == "final"
    assert f["time_bucket"] == "final"


# ---- 2. 夕食登録済み → 夕食の残り枠に触れない ----

def test_dinner_logged_never_mentions_dinner_budget(monkeypatch):
    freeze(monkeypatch, 19)
    f = F({"breakfast": 400, "lunch": 600, "dinner": 700},
          intake=1400, burn=2200)
    assert f["meal_state"]["scenario"] == "dinner_logged"
    assert f["meal_state"]["forbid_dinner_budget"] is True

    block = coach._situation_block(f)
    out = coach._rule_based(f)
    prompt = coach.build_prompt(
        {"bot_name": "アシ", "bot_pronoun": "わたし", "bot_tone": ""}, f)

    for text in (block, out):
        assert "夕食にあと" not in text
        assert "夕食にはあと" not in text
        assert "夕食は何を食べよう" not in text
    # プロンプトでも「夕食の残り枠は書かない」と明示している
    assert "夕食は登録済みなので" in prompt
    assert "夕食の献立の提案はしない" in prompt


def test_night_dinner_logged_no_dinner_budget(monkeypatch):
    freeze(monkeypatch, 22)
    f = F({"breakfast": 400, "lunch": 600, "dinner": 700}, intake=1400)
    assert f["meal_state"]["scenario"] == "night_dinner_logged"
    assert "夕食にはあと" not in coach._rule_based(f)


# ---- 3. デザートは余力があるときだけ ----

def test_dessert_only_when_calories_remain(monkeypatch):
    freeze(monkeypatch, 19)
    slots = {"breakfast": 400, "lunch": 600, "dinner": 400}

    room = F(slots, intake=1400)          # 目標1800 - 1400 = 400
    assert room["meal_state"]["dessert_room_kcal"] == 400
    block_room = coach._situation_block(room)
    assert "デザート" in block_room
    assert "目標に残っている余裕: 400kcal" in block_room
    assert "デザート" in coach._rule_based(room)

    full = F(slots, intake=1900)          # 目標超過
    assert full["meal_state"]["dessert_room_kcal"] is None
    block_full = coach._situation_block(full)
    assert "デザート" not in block_full
    assert "追加で食べる提案はせず" in block_full
    assert "今日は目標を 100kcal 超えています" in coach._rule_based(full)


def test_dessert_not_offered_below_threshold(monkeypatch):
    freeze(monkeypatch, 19)
    f = F({"breakfast": 400, "lunch": 600, "dinner": 700}, intake=1700)
    assert f["meal_state"]["dessert_room_kcal"] is None
    assert "デザート" not in coach._situation_block(f)


# ---- 4. 抜いたのか／登録がまだか を尋ねる ----

def test_before_lunch_no_breakfast_asks(monkeypatch):
    freeze(monkeypatch, 12)
    f = F({})
    assert f["meal_state"]["scenario"] == "midday_empty"
    block = coach._situation_block(f)
    assert "朝を抜いたのか" in block
    assert "登録がまだ" in block
    out = coach._rule_based(f)
    assert "朝を抜いたのか" in out and "登録がまだ" in out


def test_afternoon_no_lunch_is_busy_nudge(monkeypatch):
    freeze(monkeypatch, 14)
    f = F({"breakfast": 400})
    assert f["meal_state"]["scenario"] == "lunch_missing"
    block = coach._situation_block(f)
    assert "忙しかったのか" in block
    assert "まだ登録していない" in block


def test_lunch_logged_gives_dinner_budget(monkeypatch):
    freeze(monkeypatch, 14)
    f = F({"breakfast": 400, "lunch": 600}, intake=1000)  # 残り 800
    assert f["meal_state"]["scenario"] == "lunch_logged"
    out = coach._rule_based(f)
    assert "夕食にはあと 800kcal 使える" in out
    assert "夕食" in coach._situation_block(f)


def test_nothing_logged_by_night(monkeypatch):
    freeze(monkeypatch, 22)
    f = F({})
    assert f["meal_state"]["scenario"] == "night_empty"
    out = coach._rule_based(f)
    assert "登録し忘れ" in out
    assert "責め" not in out


# ---- 5. キャッシュキー ----

def test_cache_key_has_bucket_and_scenario(monkeypatch):
    freeze(monkeypatch, 22)
    keys = []

    monkeypatch.setattr(coach, "_generate", lambda u, f: "テストコメント")
    monkeypatch.setattr(coach, "get_report_comment", lambda k: None)
    monkeypatch.setattr(coach, "save_report_comment",
                        lambda **kw: keys.append(kw["cache_key"]))
    monkeypatch.setattr(coach, "fetch_day_summary",
                        lambda u, d: {"intake_kcal": 1700, "protein_g": 60,
                                      "fat_g": 40, "carb_g": 130, "salt_g": 3})
    monkeypatch.setattr(coach, "fetch_day_activity_total", lambda u, d: 2200)
    monkeypatch.setattr(coach, "get_goal", lambda u, d: 1800)
    monkeypatch.setattr(coach, "fetch_entries_for_date",
                        lambda u, d: [{"meal_slot": "dinner",
                                       "food_name": "カレー", "kcal": 900}])
    monkeypatch.setattr(coach, "fetch_latest_weight",
                        lambda u, on_or_before=None: None)
    monkeypatch.setattr(coach, "fetch_past_trainer_comments",
                        lambda u, before_date=None, days=14, limit=5: [])
    monkeypatch.setattr(coach, "load_user_persona",
                        lambda u: {"bot_name": "アシ", "bot_tone": "",
                                   "bot_pronoun": "わたし"})
    monkeypatch.setattr(coach, "fetch_active_directives", lambda u: [])
    monkeypatch.setattr(coach, "goal_context_line", lambda u: "")
    monkeypatch.setattr(coach, "pfc_targets", lambda u, target_kcal=None: None)

    coach.get_day_comment("U1", "2026-09-30")
    assert ":night:" in keys[0]
    assert "night_dinner_logged" in keys[0]
    assert ":night:" not in keys[0].replace(":night:", "", 1)
