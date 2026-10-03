from datetime import timedelta

from app.services.dates import today_jst_date
from app.services.meal_when import parse_meal_when

T = today_jst_date()


def iso(n):
    return (T - timedelta(days=n)).isoformat()


def test_today_lunch():
    assert parse_meal_when("今日のひる") == (iso(0), "lunch")


def test_yesterday_lunch():
    assert parse_meal_when("昨日の昼食") == (iso(1), "lunch")


def test_ototoi_breakfast():
    assert parse_meal_when("おとといの朝") == (iso(2), "breakfast")


def test_kanji_days_ago_lunch():
    assert parse_meal_when("二日前のひる") == (iso(2), "lunch")


def test_digit_days_ago_night_is_snack():
    # 夜食は間食と同じ区分（snack）で扱う
    assert parse_meal_when("2日前の夜食") == (iso(2), "snack")


def test_issakujitsu_night_is_snack():
    assert parse_meal_when("一昨日の夜") == (iso(2), "snack")


def test_iso_date():
    assert parse_meal_when("2026-10-02 昼食") == ("2026-10-02", "lunch")


def test_slash_date():
    assert parse_meal_when("10/2 昼") == ("2026-10-02", "lunch")


def test_date_only():
    assert parse_meal_when("昨日") == (iso(1), None)


def test_slot_only():
    assert parse_meal_when("ひる") == (None, "lunch")


def test_gibberish():
    assert parse_meal_when("でたらめ") == (None, None)


def test_yesterday_snack():
    assert parse_meal_when("昨日のおやつ") == (iso(1), "snack")
