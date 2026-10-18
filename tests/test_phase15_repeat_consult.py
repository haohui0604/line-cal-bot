"""Phase15: 「いついつの食事と同じ」複製登録 と 「あと何食べていい？」相談."""
from datetime import date, timedelta

from linebot.models import TextSendMessage

from app.services.dates import today_jst, today_jst_date, yesterday_jst
from app.services import repeat_meal, consult
from app.services.db import (init_db, save_entry, save_weight, set_goal,
                            fetch_day_summary, fetch_entries_for_date)
from app.handlers import text_handler

T = today_jst()
Y = yesterday_jst()


def _iso(n: int) -> str:
    return (today_jst_date() - timedelta(days=n)).isoformat()


# ---------------- 日付解決（JST基準） ----------------

def test_looks_like_repeat():
    assert repeat_meal.looks_like_repeat("昨日の昼と同じ")
    assert repeat_meal.looks_like_repeat("おとといの夕食と同じで")
    assert repeat_meal.looks_like_repeat("一昨日の朝と一緒で")
    assert not repeat_meal.looks_like_repeat("朝 食パン 250kcal")
    assert not repeat_meal.looks_like_repeat("同じくらい食べた")


def test_resolve_relative_dates_jst():
    assert repeat_meal.resolve_date("昨日の昼と同じ")[0] == _iso(1)
    assert repeat_meal.resolve_date("おとといの夕食と同じ")[0] == _iso(2)
    assert repeat_meal.resolve_date("一昨日の朝と一緒で")[0] == _iso(2)
    assert repeat_meal.resolve_date("3日前の朝と同じ")[0] == _iso(3)
    assert repeat_meal.resolve_date("今日の朝と同じ")[0] == _iso(0)


def test_resolve_weekday_last_week():
    d, _label, err = repeat_meal.resolve_date("先週の火曜の昼と同じ")
    assert err is None and d is not None
    dd = date.fromisoformat(d)
    assert dd.weekday() == 1                        # 火曜
    expect = ((today_jst_date().weekday() - 1) % 7) + 7
    assert (today_jst_date() - dd).days == expect
    assert dd <= today_jst_date()


def test_resolve_explicit_date():
    d, _label, err = repeat_meal.resolve_date("10/2の朝と同じ")
    assert err is None and d.endswith("-10-02")
    assert repeat_meal.resolve_date("2026-10-02の朝と同じ")[0] == "2026-10-02"


def test_future_date_rejected():
    d, _label, err = repeat_meal.resolve_date("来週の月曜の昼と同じ")
    assert d is None and err


def test_parse_repeat_ref_slot():
    r = repeat_meal.parse_repeat_ref("昨日の昼と同じ")
    assert r["slot"] == "lunch" and r["date"] == _iso(1)
    assert repeat_meal.parse_repeat_ref("おとといの夜食と同じ")["slot"] == "snack"
    assert repeat_meal.parse_repeat_ref("先週の水曜の夕食と同じ")["slot"] == "dinner"
    assert repeat_meal.parse_repeat_ref("昨日の朝と同じ")["slot"] == "breakfast"


# ---------------- 複製登録（DB実測） ----------------

def test_handle_repeat_copies_same_values():
    init_db()
    uid = "U_rep_1"
    save_entry(user_id=uid, date=Y, meal_slot="lunch", food_name="牛丼",
               kcal=720, protein_g=20, fat_g=18, carb_g=110, salt_g=2.5,
               source_type="user_report", confidence="estimated")
    save_entry(user_id=uid, date=Y, meal_slot="lunch", food_name="味噌汁",
               kcal=40, protein_g=3, fat_g=1.5, carb_g=4, salt_g=1.2,
               source_type="user_report", confidence="estimated")
    # 同じ日の別区分はコピー対象にならない
    save_entry(user_id=uid, date=Y, meal_slot="breakfast", food_name="トースト",
               kcal=200, source_type="user_report", confidence="estimated")

    msg = repeat_meal.handle_repeat(uid, "昨日の昼と同じ")
    assert isinstance(msg, TextSendMessage)
    assert "牛丼" in msg.text and "味噌汁" in msg.text
    assert "トースト" not in msg.text
    assert "記録しますか" in msg.text

    p = text_handler._pending[uid]
    assert len(p["foods"]) == 2
    assert p["meal_slot"] == "lunch"
    assert p["date"] == T
    assert p["foods"][0]["_source_type"] == "repeat_copy"

    out = text_handler.handle_text(uid, "はい")
    assert "記録しました" in out.text

    s = fetch_day_summary(uid, T)
    assert abs(s["intake_kcal"] - 760) < 0.01
    assert abs(s["protein_g"] - 23) < 0.01
    assert abs(s["fat_g"] - 19.5) < 0.01
    assert abs(s["salt_g"] - 3.7) < 0.01

    rows = fetch_entries_for_date(uid, T)
    got = {r["food_name"]: r for r in rows}
    assert set(got) == {"牛丼", "味噌汁"}
    assert abs(got["牛丼"]["kcal"] - 720) < 0.01
    assert abs(got["味噌汁"]["kcal"] - 40) < 0.01
    assert all(r["meal_slot"] == "lunch" for r in rows)


def test_handle_repeat_no_record():
    init_db()
    msg = repeat_meal.handle_repeat("U_rep_2", "昨日の昼と同じ")
    assert "見つかりませんでした" in msg.text


def test_handle_repeat_missing_slot(monkeypatch):
    init_db()
    monkeypatch.setattr(repeat_meal, "_llm_ref", lambda _t: None)
    uid = "U_rep_3"
    save_entry(user_id=uid, date=Y, meal_slot="lunch", food_name="X", kcal=100,
               source_type="user_report", confidence="estimated")
    msg = repeat_meal.handle_repeat(uid, "昨日と同じ")
    assert "どの食事ですか" in msg.text


def test_handle_repeat_missing_date(monkeypatch):
    init_db()
    monkeypatch.setattr(repeat_meal, "_llm_ref", lambda _t: None)
    msg = repeat_meal.handle_repeat("U_rep_4", "昼と同じ")
    assert "いつの食事か" in msg.text


# ---------------- 相談 ----------------

def test_is_consult():
    assert consult.is_consult("今日あと何食べていい？")
    assert consult.is_consult("あとどれくらい食べてもいい？")
    assert consult.is_consult("夕飯のおすすめは？")
    assert consult.is_consult("もっと軽めに")
    assert consult.is_consult("たんぱく質足りてる？")
    assert not consult.is_consult("朝 食パン 250kcal")
    assert not consult.is_consult("こんにちは")


def test_build_consult_context_numbers():
    init_db()
    uid = "U_con_1"
    set_goal(user_id=uid, date=T, target_kcal=2161)
    save_weight(user_id=uid, date=T, weight_kg=74.5, is_measured=1,
                note="test")   # PFC目標の目安計算に体重が要る
    save_entry(user_id=uid, date=T, meal_slot="breakfast", food_name="食パン",
               kcal=250, protein_g=8, fat_g=4, carb_g=45, salt_g=0.6,
               source_type="user_report", confidence="estimated")
    save_entry(user_id=uid, date=T, meal_slot="lunch", food_name="牛丼",
               kcal=720, protein_g=20, fat_g=18, carb_g=110, salt_g=2.5,
               source_type="user_report", confidence="estimated")
    ctx = consult.build_consult_context(uid)
    assert ctx["intake"] == 970
    assert ctx["burn"] == 0
    assert ctx["target"] == 2161
    assert ctx["remaining_raw"] == 1191
    assert ctx["remaining_net"] == 1191
    assert ctx["remaining_pfc"]["protein_g"] is not None
    assert any(f["name"] == "牛丼" for f in ctx["frequent_foods"])


def test_recalc_rejects_over_budget():
    budget = {"kcal": 500.0}
    opts = [
        {"title": "重い", "items": [{"name": "A", "kcal": 900, "protein_g": 30}]},
        {"title": "軽い", "items": [{"name": "B", "kcal": 300, "protein_g": 20},
                                    {"name": "C", "kcal": 100, "protein_g": 5}],
         "kcal": 9999},
    ]
    ok, over = consult._recalc_options(opts, budget)
    assert [o["title"] for o in ok] == ["軽い"]
    assert ok[0]["kcal"] == 400          # 申告値ではなく明細から再計算
    assert ok[0]["protein_g"] == 25.0
    assert [o["title"] for o in over] == ["重い"]


def test_propose_uses_code_numbers_and_filters(monkeypatch):
    init_db()
    uid = "U_con_2"
    set_goal(user_id=uid, date=T, target_kcal=2161)
    save_entry(user_id=uid, date=T, meal_slot="lunch", food_name="牛丼",
               kcal=1541, protein_g=40, fat_g=40, carb_g=200, salt_g=4,
               source_type="user_report", confidence="estimated")

    def fake(user_text, ctx, persona):
        assert ctx["budget_kcal"] == 620      # コード側の残り予算が渡る
        return {"intro": "たんぱく質が足りません。",
                "options": [
                    {"title": "鶏むねと野菜",
                     "items": [{"name": "鶏むね肉", "quantity": "150g",
                                "kcal": 248, "protein_g": 34.5, "fat_g": 4.5,
                                "carb_g": 0},
                               {"name": "ブロッコリー", "quantity": "100g",
                                "kcal": 33, "protein_g": 4.3, "fat_g": 0.5,
                                "carb_g": 5.2}],
                     "why": "たんぱく質が不足ぎみです。"},
                    {"title": "食べすぎ案",
                     "items": [{"name": "ラーメン", "kcal": 900, "protein_g": 30}],
                     "why": "x"}],
                "notes": "水分も忘れずに。"}

    monkeypatch.setattr(consult, "_call_llm", fake)
    msg = consult.propose(uid, "今日あと何食べていい？")
    assert isinstance(msg, TextSendMessage)
    assert "残り 620 kcal" in msg.text
    assert "鶏むね肉" in msg.text and "ブロッコリー" in msg.text
    assert "約281kcal" in msg.text          # 明細から再計算した合計
    assert "ラーメン" not in msg.text        # 予算超過は除外
    assert "予算を超える案 1件" in msg.text


def test_propose_fallback_without_llm(monkeypatch):
    init_db()
    uid = "U_con_3"
    set_goal(user_id=uid, date=T, target_kcal=1800)

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(consult, "_call_llm", boom)
    msg = consult.propose(uid, "あと何食べていい？")
    assert "標準案" in msg.text
    assert "鶏むね肉" in msg.text or "木綿豆腐" in msg.text


def test_handle_text_routes_both_features(monkeypatch):
    init_db()
    uid = "U_con_4"
    set_goal(user_id=uid, date=T, target_kcal=1800)

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(consult, "_call_llm", boom)
    out = text_handler.handle_text(uid, "今日あと何食べていい？")
    assert "あと食べていい量" in out.text

    # 通常の記録が壊れていないこと（回帰）
    out2 = text_handler.handle_text(uid, "朝 食パン100g 250kcal")
    assert "記録: " in out2.text
