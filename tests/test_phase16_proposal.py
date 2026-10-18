"""Phase16: 「これ食べたらどう？」提案の評価（カロリー/PFCの過不足）."""
from datetime import timedelta

from linebot.models import TextSendMessage

from app.services.dates import today_jst, today_jst_date
from app.services import consult
from app.services.db import (init_db, save_entry, save_weight, set_goal,
                            fetch_day_summary, fetch_entries_for_date)
from app.handlers import text_handler

T = today_jst()
Y = (today_jst_date() - timedelta(days=1)).isoformat()


def _mk(uid, kcal=1541, protein=40, fat=40, carb=200, salt=4, target=2161,
        weight=74.5):
    init_db()
    if weight:
        save_weight(user_id=uid, date=T, weight_kg=weight, is_measured=1, note="t")
    if target:
        set_goal(user_id=uid, date=T, target_kcal=target)
    save_entry(user_id=uid, date=T, meal_slot="lunch", food_name="牛丼",
               kcal=kcal, protein_g=protein, fat_g=fat, carb_g=carb, salt_g=salt,
               source_type="user_report", confidence="estimated")


# ---------------- 意図判定（トリガーの衝突回避） ----------------

def test_is_proposal_positive():
    for t in ["ラーメン食べたらどう？", "唐揚げ5個はアリ？", "夜はカレーにしようかな",
              "おにぎり2個でいいかな", "ラーメン食べていい？",
              "ピザ食べようかな", "これで大丈夫かな"]:
        assert consult.is_proposal(t), t


def test_is_proposal_negative_and_no_collision():
    # 相談（あと何食べていい？）は提案評価に奪われない
    for t in ["今日あと何食べていい？", "あとどれくらい食べてもいい？",
              "夕飯のおすすめは？", "もっと軽めに", "たんぱく質足りてる？"]:
        assert not consult.is_proposal(t), t
    # 記録・複製も奪わない
    assert not consult.is_proposal("朝 食パン100g 250kcal")
    assert not consult.is_proposal("ラーメン食べた")
    assert not consult.is_proposal("昨日の昼と同じ")
    assert not consult.is_proposal("こんにちは")


def test_already_eaten_goes_to_logging(monkeypatch):
    """「食べた」報告は提案評価ではなく記録フローへ（None を返す）."""
    _mk("U_p16_log")
    monkeypatch.setattr(consult, "_extract", lambda _t: {
        "ate_already": True, "foods": [{"name": "ラーメン", "quantity": "1杯"}]})
    assert consult.evaluate_proposal("U_p16_log", "ラーメン食べちゃった、どう？") is None


# ---------------- 判定ロジック（すべてコード計算） ----------------

def test_judge_over_and_protein_short():
    _mk("U_p16_j1")
    ctx = consult.build_consult_context("U_p16_j1")
    foods = [{"food_name": "ラーメン", "kcal": 470, "protein_g": 20,
              "fat_g": 15, "carb_g": 70, "salt_g": 5.5}]
    v = consult.judge_proposal(ctx, foods)
    assert v["remaining"] == 620           # 2161 - 1541
    assert v["diff"] == -150               # 470 - 620
    assert v["kcal_level"] == "under"
    assert v["after_intake"] == 2011
    assert v["protein_target"] == 119.2    # 74.5kg × 1.6
    assert v["protein_short"] == 59.2      # 119.2 - (40 + 20)
    assert "たんぱく質 59g 不足" in v["flags"]
    assert v["salt_over"] == round(5.5 - (7.5 - 4), 1)


def test_judge_kcal_over():
    _mk("U_p16_j2")
    ctx = consult.build_consult_context("U_p16_j2")
    foods = [{"food_name": "ラーメン＋餃子", "kcal": 1000, "protein_g": 35,
              "fat_g": 40, "carb_g": 110, "salt_g": 8}]
    v = consult.judge_proposal(ctx, foods)
    assert v["diff"] == 380 and v["kcal_level"] == "over"
    assert "カロリー 380kcal オーバー" in v["flags"]
    assert "脂質" in " ".join(v["flags"])   # 40+40=80 > 54


def test_judge_just_right():
    _mk("U_p16_j3")
    ctx = consult.build_consult_context("U_p16_j3")
    foods = [{"food_name": "定食", "kcal": 600, "protein_g": 80,
              "fat_g": 14, "carb_g": 30, "salt_g": 1}]
    v = consult.judge_proposal(ctx, foods)
    assert v["kcal_level"] == "just"
    assert v["protein_short"] < 0
    assert v["flags"] == []


def test_judge_no_goal_uses_default():
    init_db()
    uid = "U_p16_j4"
    save_weight(user_id=uid, date=T, weight_kg=70.0, is_measured=1, note="t")
    ctx = consult.build_consult_context(uid)
    assert ctx["target"] == 1800           # 目標未設定 → 既定値
    v = consult.judge_proposal(ctx, [{"food_name": "X", "kcal": 300,
                                      "protein_g": 10, "fat_g": 5,
                                      "carb_g": 30, "salt_g": 0.5}])
    assert v["protein_target"] == 112.0    # 70kg × 1.6
    assert v["remaining"] == 1800


# ---------------- 応答の組み立て（AIは文言のみ） ----------------

def _fake_extract(_t):
    return {"ate_already": False,
            "foods": [{"name": "ラーメン", "quantity": "1杯", "quantity_g": None},
                      {"name": "餃子", "quantity": "5個", "quantity_g": None}]}


def _fake_estimate(foods):
    out = []
    for f in foods:
        n = f.get("name")
        if n == "ラーメン":
            out.append({"food_name": n, "kcal": 470, "protein_g": 20, "fat_g": 15,
                        "carb_g": 70, "salt_g": 5.5, "quantity_g": None})
        elif n == "餃子":
            out.append({"food_name": n, "kcal": 400, "protein_g": 12, "fat_g": 20,
                        "carb_g": 40, "salt_g": 2.0, "quantity_g": None})
    return out


def _fake_phrase(_t, _ctx, _v, _p):
    return {"verdict": "カロリーは少し余裕がありますが、たんぱく質が足りません。",
            "adjustments": [{"label": "チャーシューを鶏むねに", "detail": "たんぱく質が増えます",
                             "save_kcal": 120}],
            "notes": "スープは残すと食塩を抑えられます。"}


def test_evaluate_proposal_full(monkeypatch):
    _mk("U_p16_e1")
    monkeypatch.setattr(consult, "_extract", _fake_extract)
    monkeypatch.setattr(consult, "_estimate", _fake_estimate)
    monkeypatch.setattr(consult, "_phrase", _fake_phrase)
    msg = consult.evaluate_proposal("U_p16_e1", "ラーメンと餃子5個食べたらどう？")
    assert isinstance(msg, TextSendMessage)
    t = msg.text
    assert "「ラーメン＋餃子」" in t
    assert "合計 約870kcal" in t                 # 470+400（明細から再計算）
    assert "P32 F35 C110 食塩7.5g" in t
    assert "今日の摂取 1541kcal → 足すと 2411kcal（目標 2161kcal）" in t
    assert "250kcal オーバー" in t               # 870 - 620
    assert "たんぱく質が 47g 足りません（目標 119g）" in t
    assert "調整するなら" in t and "チャーシューを鶏むねに" in t
    assert "約120kcal 減" in t
    assert consult._last_proposal["U_p16_e1"]["totals"]["kcal"] == 870.0


def test_evaluate_proposal_unparseable(monkeypatch):
    _mk("U_p16_e2")
    monkeypatch.setattr(consult, "_extract", lambda _t: {"foods": []})
    msg = consult.evaluate_proposal("U_p16_e2", "なんか食べたらどう？")
    assert "読み取れませんでした" in msg.text


def test_evaluate_proposal_estimate_failed(monkeypatch):
    _mk("U_p16_e3")
    monkeypatch.setattr(consult, "_extract", _fake_extract)
    monkeypatch.setattr(consult, "_estimate", lambda _f: [])
    msg = consult.evaluate_proposal("U_p16_e3", "ラーメン食べたらどう？")
    assert "推定できませんでした" in msg.text


def test_evaluate_proposal_without_ai(monkeypatch):
    _mk("U_p16_e4")
    monkeypatch.setattr(consult, "_extract", _fake_extract)
    monkeypatch.setattr(consult, "_estimate", _fake_estimate)

    def boom(*_a, **_k):
        raise RuntimeError("no ai")

    monkeypatch.setattr(consult, "_phrase", boom)
    msg = consult.evaluate_proposal("U_p16_e4", "ラーメンと餃子5個食べたらどう？")
    assert "合計 約870kcal" in msg.text
    assert "計算結果のみ表示" in msg.text


# ---------------- 提案後の操作 ----------------

def test_followup_record(monkeypatch):
    _mk("U_p16_f1")
    monkeypatch.setattr(consult, "_extract", _fake_extract)
    monkeypatch.setattr(consult, "_estimate", _fake_estimate)
    monkeypatch.setattr(consult, "_phrase", _fake_phrase)
    consult.evaluate_proposal("U_p16_f1", "ラーメンと餃子5個食べたらどう？")

    assert consult.is_proposal_followup("U_p16_f1", "記録する")
    msg = consult.handle_followup("U_p16_f1", "記録する")
    assert "記録しますか" in msg.text
    out = text_handler.handle_text("U_p16_f1", "はい")
    assert "記録しました" in out.text

    rows = fetch_entries_for_date("U_p16_f1", T)
    got = {r["food_name"]: r for r in rows}
    assert "ラーメン" in got and "餃子" in got
    s = fetch_day_summary("U_p16_f1", T)
    assert s["intake_kcal"] == 1541 + 870
    assert consult._last_proposal.get("U_p16_f1") is None


def test_followup_lighten(monkeypatch):
    _mk("U_p16_f2")
    monkeypatch.setattr(consult, "_extract", _fake_extract)
    monkeypatch.setattr(consult, "_estimate", _fake_estimate)
    monkeypatch.setattr(consult, "_phrase", _fake_phrase)
    consult.evaluate_proposal("U_p16_f2", "ラーメンと餃子5個食べたらどう？")

    seen = {}

    def cap(user_text, ctx, persona):
        seen["budget"] = ctx["budget_kcal"]
        seen["considered"] = ctx.get("considered")
        seen["constraint"] = ctx.get("constraint")
        return {"intro": "", "options": [
            {"title": "軽め", "items": [{"name": "豆腐", "kcal": 110, "protein_g": 10}],
             "why": "軽いです"}], "notes": ""}

    monkeypatch.setattr(consult, "_call_llm", cap)
    msg = consult.handle_followup("U_p16_f2", "もっと軽く")
    assert seen["budget"] == 310.0          # 残り620の半分
    assert seen["considered"] == "ラーメン＋餃子"
    assert "軽め" in seen["constraint"]
    assert "あと食べていい量" in msg.text


def test_followup_stop():
    _mk("U_p16_f3")
    consult._last_proposal["U_p16_f3"] = {"at": 9e18, "foods": [], "label": "x"}
    msg = consult.handle_followup("U_p16_f3", "やめとく")
    assert "了解" in msg.text
    assert "U_p16_f3" not in consult._last_proposal


def test_followup_requires_prior_proposal():
    assert not consult.is_proposal_followup("U_none", "記録する")
    assert consult.handle_followup("U_none", "記録する") is None


# ---------------- ルーティングと既存フローの回帰 ----------------

def test_handle_text_routes_proposal(monkeypatch):
    _mk("U_p16_r1")
    monkeypatch.setattr(consult, "_extract", _fake_extract)
    monkeypatch.setattr(consult, "_estimate", _fake_estimate)
    monkeypatch.setattr(consult, "_phrase", _fake_phrase)
    out = text_handler.handle_text("U_p16_r1", "ラーメンと餃子5個食べたらどう？")
    assert "食べたら？（今日の記録から計算）" in out.text


def test_regression_consult_and_repeat(monkeypatch):
    _mk("U_p16_r2")

    def boom(*_a, **_k):
        raise RuntimeError("no ai")

    monkeypatch.setattr(consult, "_call_llm", boom)
    # 「あと何」相談が生きている
    out = text_handler.handle_text("U_p16_r2", "今日あと何食べていい？")
    assert "あと食べていい量" in out.text

    # 「同じ」複製が生きている（今日の昼をコピー）
    out2 = text_handler.handle_text("U_p16_r2", "今日の昼と同じ")
    assert "と同じ内容を" in out2.text and "牛丼" in out2.text

    # 通常の記録も壊れていない
    out3 = text_handler.handle_text("U_p16_r2", "朝 食パン100g 250kcal")
    assert "記録: " in out3.text
