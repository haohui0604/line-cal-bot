from app.services.calorie_calc import parse_record_line


def test_basic_lunch_record():
    r = parse_record_line("昼 ルーローハン81g 食塩2.8g P7 F5.7 C59.2")
    assert r["meal_slot"] == "lunch"
    assert "ルーローハン" in r["food_name"]
    assert r["protein_g"] == 7.0
    assert r["fat_g"] == 5.7
    assert r["carb_g"] == 59.2
    assert r["salt_g"] == 2.8
    assert r["quantity_g"] == 81.0


def test_breakfast_protein():
    r = parse_record_line("朝 Milimホエイ25g")
    assert r["meal_slot"] == "breakfast"
    assert r["quantity_g"] == 25.0
    assert r["food_name"].startswith("Milim")


def test_no_slot_returns_none():
    assert parse_record_line("ルーローハン81g") is None
