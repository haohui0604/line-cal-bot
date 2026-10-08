"""ジム / トレーナー向けのAIコメント (Phase 10).

方針:
  - LLM に渡すのは「集計値」だけ。個人の食事名や自由記述は渡さない。
  - 出力はテキスト（見出し＋本文）。JSON崩れのリスクを避ける。
  - 生成結果は report_comments テーブルにキャッシュする。
    ジムレポートは ISO週（または月）ごとに1回、
    トレーナーの「次の一手」は1日1回（会員の組み合わせが変われば再生成）。
  - APIキー未設定 / 失敗時は、集計値から組み立てた定型文にフォールバックする。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

from app.services.dates import today_jst_date

logger = logging.getLogger(__name__)

MODELS = ("gemini-flash-latest", "gemini-2.0-flash", "gemini-flash-lite-latest")
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
TIMEOUT_SEC = 14.0
TEMPERATURE = 0.5
MAX_OUTPUT_TOKENS = 1400

SCOPE_GYM = "gym_report"
SCOPE_TRAINER = "trainer_suggest"


# ---- Gemini 呼び出し ----

def _api_key() -> str:
    try:
        from app.config import settings
        k = getattr(settings, "GEMINI_API_KEY", "") or ""
        if k:
            return str(k)
    except Exception:
        pass
    try:
        from app.config import GEMINI_API_KEY as K  # type: ignore
        if K:
            return str(K)
    except Exception:
        pass
    return os.environ.get("GEMINI_API_KEY", "")


def _post(system_text: str, user_text: str) -> str:
    key = _api_key()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    payload = {
        "systemInstruction": {"parts": [{"text": system_text}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            "temperature": TEMPERATURE,
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    last = None
    for model in MODELS:
        try:
            r = httpx.post(f"{API_BASE}/{model}:generateContent?key={key}",
                           json=payload, timeout=TIMEOUT_SEC)
            if r.status_code == 404:
                last = f"{model}:404"
                continue
            r.raise_for_status()
            cand = (r.json().get("candidates") or [{}])[0]
            parts = (cand.get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts).strip()
            if len(text) < 10:
                last = f"{model}:empty"
                continue
            return text
        except Exception as e:  # noqa: BLE001
            last = f"{model}:{type(e).__name__}"
            logger.warning("insights model %s failed: %s", model, e)
    raise RuntimeError(f"all models failed: {last}")


# ---- キャッシュ ----

def period_key(period: str = "week") -> str:
    t = today_jst_date()
    if period == "month":
        return t.strftime("%Y-%m")
    iso = t.isocalendar()
    return "%04d-W%02d" % (iso[0], iso[1])


def _key(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    try:
        from app.services import db
        return db.get_report_comment(key)
    except Exception:
        return None


def _cache_set(key: str, user_id: str, scope: str, pk: str,
               text: str, source: str, facts: Dict[str, Any]) -> None:
    try:
        from app.services import db
        db.save_report_comment(key, user_id, scope, pk, "", text, "",
                               source, json.dumps(facts, ensure_ascii=False,
                                                  default=str))
    except Exception as e:  # noqa: BLE001
        logger.warning("insights cache save skipped: %s", e)


def _render_facts(facts: Dict[str, Any]) -> str:
    lines = []
    for k, v in facts.items():
        if isinstance(v, (list, tuple)):
            v = " / ".join(str(x) for x in v)
        elif isinstance(v, dict):
            v = " / ".join(f"{kk}:{vv}" for kk, vv in v.items())
        if v is None or v == "":
            v = "—"
        lines.append(f"- {k}: {v}")
    return "\n".join(lines)


# ---- プロンプト ----

GYM_SYSTEM = """あなたはフィットネスジムの運営コンサルタントです。
与えられた「確定値」だけを根拠に、ジムの管理者向けの運営レポートを日本語で書きます。

# 絶対ルール
1. 数値は与えられたものをそのまま使う。自分で計算・換算・創作しない。
2. 与えられていない事実（会員の氏名・食事内容・疾患・天気・予定）を書かない。
3. 医療的な診断や効果の断定を書かない。
4. 個人を責める書き方をしない。事実 → 次の一手 の順で書く。
5. 出力は次の4つの見出しだけ。各見出し1〜2文、全体400字以内。絵文字は各見出しに1個まで。

【今週の傾向】
【良かった点】
【注意点】
【来週やること】"""

GYM_USER = """# 集計スコープ
{period_label} のジム運営レポート

# 確定値（この数値だけを使う）
{facts}

# 書き方
- 「継続率」と「離脱予兆」は運営の最重要指標。必ず触れる。
- 目的別人数に偏りがあれば触れる。
- 「来週やること」は、明日から実行できる運営アクションを1〜2個、命令形で書く。
- 数値を自分で再計算しない。与えられた数値の引用だけにする。"""

TRAINER_SYSTEM = """あなたはパーソナルトレーナーの指導アシスタントです。
与えられた「確定値」だけを根拠に、担当会員それぞれへの「次の一手」を日本語で書きます。

# 絶対ルール
1. 数値は与えられたものをそのまま使う。計算・創作しない。
2. 与えられていない事実（食事内容・体調・予定・疾患）を書かない。
3. 医療的な断定を書かない。
4. 会員を責めない。事実 → 提案 → 送る文面 の順で書く。
5. 出力は会員ごとに次の3行。会員は「■ 名前」で始める。全体500字以内。

■ 会員名
根拠: （与えられた数値だけを1文で）
提案: （明日すぐ実行できる具体的な行動を1つ）
文面: （そのままLINEで送れる1〜2文。やわらかい敬体）"""

TRAINER_USER = """# 対象
担当会員のうち、優先して声をかけるべき上位{count}名

# 確定値（この数値だけを使う）
{items}

# 書き方
- 最終記録からの経過日数が大きい会員ほど優先度が高い。
- 記録がない会員には「記録のハードルを下げる」提案を書く。
- 目標との差がプラス（摂取超過）の会員には、次の食事で調整する提案を書く。
- 目標が未設定の会員には、目的設定を案内する提案を書く。"""


# ---- フォールバック（LLMを使わない定型文） ----

def _g(k: str, d: str = "—") -> str:
    return d


def _gym_fallback(facts: Dict[str, Any]) -> str:
    def g(k, d="—"):
        v = facts.get(k)
        return d if v is None or v == "" else v
    return (
        "【今週の傾向】\n"
        f"対象会員は{g('会員数')}名、うち直近7日に記録したのは{g('7日記録あり')}名"
        f"（ライト継続率 {g('ライト継続率')}）です。\n"
        f"目的別の構成は{g('目的別人数')}で、期間内の食事記録は{g('期間内の食事記録数')}件でした。\n\n"
        "【良かった点】\n"
        f"7日で3日以上記録した「定着」の会員は{g('定着人数')}名（定着率 {g('定着率')}）です。\n"
        f"コメント数が最も多いのは{g('コメント数トップ')}です。\n\n"
        "【注意点】\n"
        f"7日以上記録が無い会員が{g('離脱予兆(赤)')}名、3日以上で{g('離脱予兆(黄)')}名います。\n"
        f"未返信のコメントは{g('未返信コメント数')}件です。\n\n"
        "【来週やること】\n"
        "離脱予兆の会員に、1通だけ短い声かけを送ってください。\n"
        "記録が止まっている会員には「体重だけ・写真だけでも良い」と伝えるのが有効です。\n\n"
        "（AIキー未設定または生成失敗のため、集計値から自動生成した要約です）"
    )


def _trainer_fallback(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "【次の一手】\n担当会員がいないため、提案はありません。"
    blocks = ["【次の一手】"]
    for it in items:
        idle = it.get("経過日数")
        if idle is None:
            head = "まだ記録がありません。"
        elif idle >= 7:
            head = f"最終記録から{idle}日空いています。"
        elif idle >= 3:
            head = f"最終記録から{idle}日です。"
        else:
            head = "記録は続いています。"
        diff = it.get("目標との差")
        if diff is None:
            body = "目標が未設定です。まず目的設定を案内しましょう。"
            msg = "おつかれさまです。目標を決めるとアドバイスが具体的になります。ボットに「目的設定」と送ってみてください。"
        elif diff > 0:
            body = f"目標との差が+{int(diff)}kcalです。次の食事で調整する提案を1つ。"
            msg = "おつかれさまです。今日は少しオーバー気味なので、次の食事は軽めにしてみましょう。"
        else:
            body = "目標内に収まっています。この調子と一言返しましょう。"
            msg = "おつかれさまです。いいペースです、この調子でいきましょう。"
        blocks.append(
            f"■ {it.get('name')}\n"
            f"根拠: {head}直近7日の記録は{it.get('直近7日の記録日数')}日。\n"
            f"提案: {body}\n"
            f"文面: {msg}")
    return "\n\n".join(blocks)


# ---- ジムレポート ----

def gym_report(gym_id: int, facts: Dict[str, Any], period: str = "week",
               allow_generate: bool = False) -> Dict[str, Any]:
    """ジム運営のAIレポート（週次/月次・キャッシュ付き）."""
    pk = period_key(period)
    key = _key(SCOPE_GYM, str(gym_id), pk)
    cached = _cache_get(key)
    if cached and (cached.get("comment") or "").strip():
        return {"available": True, "text": cached["comment"],
                "source": cached.get("source") or "cache", "period_key": pk}
    if not allow_generate:
        return {"available": False, "period_key": pk, "source": None, "text": ""}
    user_text = GYM_USER.format(period_label=pk, facts=_render_facts(facts))
    try:
        text = _post(GYM_SYSTEM, user_text)
        source = "ai"
    except Exception as e:  # noqa: BLE001
        logger.warning("gym_report fallback: %s", e)
        text = _gym_fallback(facts)
        source = "rule"
    _cache_set(key, f"gym:{gym_id}", SCOPE_GYM, pk, text, source, facts)
    return {"available": True, "text": text, "source": source, "period_key": pk}


# ---- トレーナーの次の一手 ----

def trainer_suggest(staff_id: str, items: List[Dict[str, Any]],
                    allow_generate: bool = False) -> Dict[str, Any]:
    """担当会員への「次の一手」（1日1回キャッシュ）."""
    pk = today_jst_date().isoformat()
    sig = ",".join(sorted(str(i.get("user_id") or "") for i in (items or [])))
    key = _key(SCOPE_TRAINER, str(staff_id), pk, sig)
    cached = _cache_get(key)
    if cached and (cached.get("comment") or "").strip():
        return {"available": True, "text": cached["comment"],
                "source": cached.get("source") or "cache", "period_key": pk}
    if not allow_generate:
        return {"available": False, "period_key": pk, "source": None, "text": ""}
    if not items:
        text = _trainer_fallback(items)
        return {"available": True, "text": text, "source": "rule",
                "period_key": pk}
    rendered = "\n\n".join(
        "\n".join(f"- {k}: {v}" for k, v in it.items()) for it in items)
    user_text = TRAINER_USER.format(count=len(items), items=rendered)
    try:
        text = _post(TRAINER_SYSTEM, user_text)
        source = "ai"
    except Exception as e:  # noqa: BLE001
        logger.warning("trainer_suggest fallback: %s", e)
        text = _trainer_fallback(items)
        source = "rule"
    _cache_set(key, f"trainer:{staff_id}", SCOPE_TRAINER, pk, text, source,
               {"items": items})
    return {"available": True, "text": text, "source": source, "period_key": pk}
