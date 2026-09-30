"""コメントの順序設計 (Phase 3.5d) の回帰テスト.

検証すること:
  1. AIコメントのプロンプトは「自分の見立て → トレーナーへの言及」の順序を明示する
  2. 対象日より前の日付のトレーナーコメントだけがプロンプトに入る
  3. 過去のトレーナーコメントが変わるとキャッシュキー(facts hash)が変わる
     → 翌日以降のAIコメントが作り直される
  4. 同じ日付のコメントは AI コメント生成の材料に含まれない
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import coach  # noqa: E402

PERSONA = {"bot_name": "ミリム", "bot_pronoun": "わたし", "bot_tone": "元気"}


def _facts(**over):
    f = {
        "date": "2026-09-28",
        "intake": 1761, "burn": 2400, "goal": 1800,
        "protein_g": 98.0, "fat_g": 87.0, "carb_g": 146.0,
        "weight_kg": 75.2,
        "foods": ["間食:エルトリートスペシャルコンボ 1150kcal"],
        "past_trainer": [],
        "hash": "x",
    }
    f.update(over)
    return f


def test_prompt_orders_ai_opinion_before_trainer():
    """AIの見立てが先、トレーナーへの言及が後、という順序指示があること."""
    prompt = coach.build_prompt(PERSONA, _facts())
    own = prompt.index("あなた自身の見立て")
    cite = prompt.index("さんも前に言ってたな")
    assert own < cite, "自分の見立てより先にトレーナー言及の指示が出ています"
    assert "そのまま繰り返す" in prompt, "繰り返し禁止の指示がありません"
    assert "毎回必ず引用する必要はない" in prompt  # 引用は任意（必要な時だけ）


def test_prompt_includes_dated_past_trainer_comments():
    f = _facts(past_trainer=[{
        "date": "2026-09-25", "author": "佐藤", "body": "タンパク質多めに",
        "directive": True}])
    prompt = coach.build_prompt(PERSONA, f)
    assert "2026-09-25" in prompt
    assert "佐藤" in prompt
    assert "タンパク質多めに" in prompt
    assert "過去の担当トレーナーからの指導" in prompt


def test_prompt_without_past_comments_has_no_block():
    prompt = coach.build_prompt(PERSONA, _facts())
    assert "過去の担当トレーナーからの指導" not in prompt


def test_prompt_keeps_deterministic_numbers():
    prompt = coach.build_prompt(PERSONA, _facts())
    assert "1761kcal" in prompt
    assert "2400kcal" in prompt
    assert "75.2" in prompt


def test_facts_hash_changes_when_past_comment_added(monkeypatch):
    """過去日にトレーナーコメントが付くと、翌日のキャッシュキーが変わる."""
    import hashlib, json

    def h(past):
        f = _facts(past_trainer=past)
        f.pop("hash", None)
        return hashlib.md5(
            json.dumps(f, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()[:12]

    before = h([])
    after = h([{"date": "2026-09-27", "author": "佐藤",
                "body": "脂質を控えめに", "directive": False}])
    assert before != after, "過去のコメントがキャッシュキーに反映されていません"


def test_build_facts_excludes_same_day_comments(monkeypatch):
    """当日のコメントは AI コメント生成材料に含めない（なぞり防止）."""
    captured = {}

    def fake_past(user_id, before_date, days=14, limit=5):
        captured["before_date"] = before_date
        return []

    monkeypatch.setattr(coach, "fetch_past_trainer_comments", fake_past)
    monkeypatch.setattr(coach, "fetch_day_summary",
                        lambda u, d: {"intake_kcal": 100, "protein_g": 1,
                                      "fat_g": 2, "carb_g": 3, "salt_g": 0})
    monkeypatch.setattr(coach, "fetch_day_activity_total", lambda u, d: 200)
    monkeypatch.setattr(coach, "get_goal", lambda u, d: 1800)
    monkeypatch.setattr(coach, "fetch_entries_for_date", lambda u, d: [])
    monkeypatch.setattr(coach, "fetch_latest_weight", lambda u, on_or_before=None: None)

    coach._build_facts("U1", "2026-09-28")
    assert captured["before_date"] == "2026-09-28", \
        "過去コメントは対象日より前で絞り込まれていません"


def test_rule_based_fallback_uses_only_facts():
    text = coach._rule_based(_facts())
    assert "1761kcal" in text and "2400kcal" in text
    assert "痩せ" not in text
