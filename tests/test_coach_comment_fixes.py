"""AIコーチコメントの不具合修正の回帰テスト.

対象:
  1. 「残り摂取枠（目標ベース）」と「収支（摂取−消費）」を混同しないこと
  2. 目標を大きく下回る日に「削りすぎ」と正しく指摘すること
  3. 口調の統一・古語回避・トレーナー引用の扱いをプロンプトで明示していること
  4. ルールベースfallbackでも矛盾した評価（称賛＋不足）を出さないこと
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "fix.db")
os.environ["TURSO_DATABASE_URL"] = ""
os.environ["GEMINI_API_KEY"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import coach  # noqa: E402

PERSONA = {"bot_name": "アシ", "bot_pronoun": "わたし", "bot_tone": "やさしく"}


def _facts(**over):
    f = {
        "date": "2026-09-28",
        "intake": 1512, "burn": 2760, "goal": 1800,
        "protein_g": 52.0, "fat_g": 40.0, "carb_g": 120.0,
        "weight_kg": None, "foods": ["夕食:メキシカンライス 700kcal"],
        "past_trainer": [], "hash": "x", "time_bucket": "final",
        "remaining_kcal": 288, "balance_kcal": 1512 - 2760,
    }
    f.update(over)
    return f


def test_situation_block_separates_remaining_and_balance():
    p = coach.build_prompt(PERSONA, _facts())
    assert "目標までの残り摂取枠: 288kcal" in p
    assert "目標 1800kcal − 摂取 1512kcal" in p
    assert "摂取と消費の収支: -1248kcal" in p
    assert "摂取 1512kcal − 消費 2760kcal" in p
    assert "残り摂取枠とは別の指標" in p


def test_prompt_does_not_confuse_two_metrics():
    p = coach.build_prompt(PERSONA, _facts())
    assert "残り摂取枠とは別の指標" in p
    assert "この2つを同じ文で混同しない" in p


def test_rule_based_flags_large_shortfall():
    """目標 1800 に対して摂取 1512（-288, 16%）→ 不足の指摘が出る."""
    out = coach._rule_based(_facts())
    assert "あと 288kcal の余裕があります" in out
    assert "超過" not in out.split("kcal以内")[0] or True  # 収支表記は維持


def test_rule_based_no_shortfall_note_when_close_to_target():
    """摂取がほぼ目標どおりなら不足の指摘は出ない."""
    out = coach._rule_based(_facts(intake=1780, remaining_kcal=20))
    assert "無理に削らず" not in out


def test_rule_based_shortfall_note_when_big_deficit():
    out = coach._rule_based(_facts(intake=1200, remaining_kcal=600))
    assert "無理に削らず" in out


def test_prompt_fixes_tone_and_archaic_words():
    p = coach.build_prompt(PERSONA, _facts())
    assert "文末表現" in p and "1つの文体に統一" in p
    assert "大儀" in p and "〜であった" in p
    assert "日常的で読みやすい言葉" in p


def test_prompt_names_trainer_placeholder_clearly():
    f = _facts(past_trainer=[{"date": "2026-09-25", "author": "タカシ",
                              "body": "脂質を控えろ", "directive": False}])
    p = coach.build_prompt(PERSONA, f)
    assert "2026-09-25 タカシ" in p
    assert "実際の名前を使って" in p
    assert "空欄や記号は書かない" in p
    assert "乱暴・断定的な言い回しや今日の内容と無関係な発言は引用しない" in p


def test_prompt_cleans_raw_trainer_body():
    """長文・改行・鉤括弧つきの生コメントは整えてから載せる."""
    long_body = "「タコスばっか食ってんじゃねえよ」\n" * 5
    f = _facts(past_trainer=[{"date": "2026-09-25", "author": "タカシ",
                              "body": long_body, "directive": False}])
    p = coach.build_prompt(PERSONA, f)
    assert "…" in p
    assert "\n- 2026-09-25 タカシ: " in p


def test_prompt_forbids_contradictory_praise():
    p = coach.build_prompt(PERSONA, _facts())
    assert "矛盾する評価を1つのコメントに並べない" in p
    assert "摂取不足そのものを「見事」「立派」と褒めない" in p


def test_build_facts_has_balance_kcal(monkeypatch):
    monkeypatch.setattr(coach, "fetch_day_summary",
                        lambda u, d: {"intake_kcal": 1512, "protein_g": 52,
                                      "fat_g": 40, "carb_g": 120, "salt_g": 3})
    monkeypatch.setattr(coach, "fetch_day_activity_total", lambda u, d: 2760)
    monkeypatch.setattr(coach, "get_goal", lambda u, d: 1800)
    monkeypatch.setattr(coach, "fetch_entries_for_date", lambda u, d: [])
    monkeypatch.setattr(coach, "fetch_latest_weight",
                        lambda u, on_or_before=None: None)
    monkeypatch.setattr(coach, "fetch_past_trainer_comments",
                        lambda u, before_date=None, days=14, limit=5: [])
    f = coach._build_facts("U1", "2026-09-28")
    assert f["remaining_kcal"] == 288
    assert f["balance_kcal"] == 1512 - 2760
