"""「今日あと何を食べていい？」の相談に、実データから具体メニューを返す.

設計方針:
- 残りkcal・残りPFC・履歴の集計は **コードで計算** する（AIに数値を作らせない）。
- AIには解釈と献立の言語化だけを任せ、返ってきた各案の合計はコードで再計算する。
- 残り予算を超える案はコードで除外する（AIの出力をそのまま信じない）。
- 失敗時はルールベースの標準案にフォールバックする（無応答にしない）。
"""
import logging
import re
import time
from datetime import timedelta

from linebot.models import TextSendMessage

from app.services.dates import today_jst, today_jst_date

logger = logging.getLogger(__name__)

_HISTORY_DAYS = 28
_AVG_DAYS = 14
_MIN_BUDGET = 100.0      # これ未満のときは「軽め枠」として 200kcal まで許容
_LIGHT_CAP = 200.0

# 相談とみなす言い回し
_CONSULT_PAT = re.compile(
    r"あと\s*(何|どれ|どのくら|どれくら)|残り|どれくらい|どれぐらい|どれだけ|"
    r"何を食べ|なにを食べ|何食べ|なに食べ|おすすめ|オススメ|メニュー|献立|"
    r"提案|相談|足りてる|足りない|食べていい|食べてもいい|"
    r"(朝|昼|夕|夜|晩)(ごはん|ご飯|飯|食)\s*何|"
    r"間食.{0,8}(何|おすすめ)|軽め|がっつり|別の案|他の案|間食で"
)


def is_consult(text: str) -> bool:
    """「あと何食べていい？」系の相談か."""
    return bool(_CONSULT_PAT.search(text or ""))


# 「これ食べたらどう？」= 提案の評価。相談・複製とはトリガーを分ける。
# 「あと何／どれくらい／おすすめ」などは相談側に残す（提案評価に奪わせない）
_CONSULT_ONLY = re.compile(
    r"あと\s*(何|どれ|どのくら|どれくら)|残り|どれくらい|どれぐらい|どれだけ|"
    r"おすすめ|オススメ|メニュー|献立|提案して|相談|足りて|足りな|"
    r"何を食べ|なにを食べ|何食べ|なに食べ|軽め|がっつり|別の案|他の案"
)
_PROPOSAL_PAT = re.compile(
    r"たらどう|たらどお|どうかな|どうですか|どうでしょう|どう？|どう\?|"
    r"はアリ|アリかな|ありかな|アリ？|アリ\?|"
    r"にしようかな|にしよっかな|にしようと|にしよかな|にしようか|"
    r"食べようかな|食べよっかな|食べちゃお|食べようと|飲もうかな|"
    r"でいいかな|でいいですか|で大丈夫|いけそう|大丈夫かな|"
    r"食べていい|食べてもいい|オーバーかな"
)


def is_proposal(text: str) -> bool:
    """「これ食べたらどう？」という“提案”への評価依頼か.

    相談（あと何食べていい？）や複製（〜と同じ）とはトリガーを分ける。
    """
    t = text or ""
    if _CONSULT_ONLY.search(t):
        return False
    return bool(_PROPOSAL_PAT.search(t))


# 提案のあとに押される操作（msq のテキストで戻ってくる）
_FOLLOWUP_TEXTS = ("記録する", "もっと軽く", "軽くする", "やめとく", "やっぱやめる")


def is_proposal_followup(user_id: str, text: str) -> bool:
    """直前の提案に対する操作か（提案が無ければ False）."""
    if not _last_proposal.get(user_id):
        return False
    return (text or "").strip() in _FOLLOWUP_TEXTS


def _num(v):
    return float(v) if isinstance(v, (int, float)) else None


# ---------- 事実（すべてコードで計算） ----------

def frequent_foods(user_id: str, days: int = _HISTORY_DAYS, limit: int = 12):
    """よく食べている食品（回数順）."""
    from app.services.db import get_conn
    start = (today_jst_date() - timedelta(days=days)).isoformat()
    try:
        with get_conn() as c:
            rows = c.execute(
                "SELECT food_name, COUNT(*) AS n, ROUND(AVG(kcal)) AS kcal"
                " FROM entries WHERE user_id=? AND date>=?"
                " GROUP BY food_name ORDER BY n DESC, food_name LIMIT ?",
                (user_id, start, limit)).fetchall()
    except Exception:
        logger.warning("frequent_foods failed", exc_info=True)
        return []
    out = []
    for r in rows:
        try:
            out.append({"name": r["food_name"], "count": int(r["n"]),
                        "avg_kcal": float(r["kcal"] or 0)})
        except Exception:
            pass
    return out


def slot_counts(user_id: str, days: int = _HISTORY_DAYS):
    """食事区分ごとの記録件数."""
    from app.services.db import get_conn
    start = (today_jst_date() - timedelta(days=days)).isoformat()
    try:
        with get_conn() as c:
            rows = c.execute(
                "SELECT meal_slot, COUNT(*) AS n FROM entries"
                " WHERE user_id=? AND date>=? GROUP BY meal_slot",
                (user_id, start)).fetchall()
    except Exception:
        return {}
    out = {}
    for r in rows:
        try:
            out[str(r["meal_slot"])] = int(r["n"])
        except Exception:
            pass
    return out


def build_consult_context(user_id: str) -> dict:
    """相談に必要な数値と傾向をすべて集める（AIにはこれを渡すだけ）."""
    from app.services.db import (fetch_day_summary, fetch_recent_history,
                                 fetch_today_food_names, get_goal)
    from app.services import goals as goal_svc

    today = today_jst()
    s = fetch_day_summary(user_id, today)
    intake = float(s.get("intake_kcal") or 0)
    burn = float(s.get("burn_kcal") or 0)

    target = None
    try:
        g = get_goal(user_id, today)
        target = float(g) if g is not None else None
    except Exception:
        target = None
    prof = goal_svc.get_profile(user_id) or {}
    if not target:
        target = _num(prof.get("calc_target_kcal"))
    target = float(target or 1800.0)

    pfc_t = goal_svc.pfc_targets(user_id, target_kcal=target) or {}
    now = {"protein_g": float(s.get("protein_g") or 0),
           "fat_g": float(s.get("fat_g") or 0),
           "carb_g": float(s.get("carb_g") or 0)}
    rem_pfc = {}
    for k in ("protein_g", "fat_g", "carb_g"):
        tv = _num(pfc_t.get(k))
        if tv is not None:
            rem_pfc[k] = round(tv - now[k], 1)

    salt_target = _num(prof.get("salt_target_g")) or 7.5
    salt_now = float(s.get("salt_g") or 0)

    avg_intake = None
    try:
        hist = fetch_recent_history(user_id, days=_AVG_DAYS) or []
        vals = [float(h.get("intake_kcal") or 0) for h in hist
                if (h.get("intake_kcal") or 0) > 0]
        if vals:
            avg_intake = sum(vals) / len(vals)
    except Exception:
        hist = []

    weight = None
    try:
        from app.services.db import fetch_latest_weight
        lw = fetch_latest_weight(user_id, today)
        weight = lw["weight_kg"] if lw else None
    except Exception:
        weight = None

    goal_line = ""
    try:
        goal_line = goal_svc.context_line(user_id) or ""
    except Exception:
        goal_line = ""

    return {
        "today": today,
        "intake": intake,
        "burn": burn,
        "target": target,
        "remaining_raw": round(target - intake, 1),
        "remaining_net": round(target + burn - intake, 1),
        "pfc_now": now,
        "pfc_target": pfc_t,
        "remaining_pfc": rem_pfc,
        "salt_now": salt_now,
        "salt_target": salt_target,
        "salt_remaining": round(max(salt_target - salt_now, 0), 1),
        "goal_line": goal_line,
        "weight": weight,
        "avg_intake": round(avg_intake) if avg_intake else None,
        "frequent_foods": frequent_foods(user_id),
        "slot_counts": slot_counts(user_id),
        "today_foods": fetch_today_food_names(user_id, today) or [],
    }


# ---------- AIの出力を検算・選別 ----------

def _recalc_options(options, budget: dict):
    """各案の合計をコードで再計算し、予算超過の案を分離する.

    返り値: (採用案, 予算超過で除外した案)
    """
    cap = float(budget.get("kcal") or 0) * 1.05
    ok, over = [], []
    for o in (options or []):
        if not isinstance(o, dict):
            continue
        items = o.get("items") or []
        clean = []
        k = p = f = c = 0.0
        for it in items:
            if not isinstance(it, dict):
                continue
            name = str(it.get("name") or "").strip()
            if not name:
                continue
            ki = _num(it.get("kcal")) or 0.0
            pi = _num(it.get("protein_g")) or 0.0
            fi = _num(it.get("fat_g")) or 0.0
            ci = _num(it.get("carb_g")) or 0.0
            k += ki
            p += pi
            f += fi
            c += ci
            clean.append({"name": name[:30],
                          "quantity": str(it.get("quantity") or "").strip()[:12],
                          "kcal": ki, "protein_g": pi, "fat_g": fi,
                          "carb_g": ci, "salt_g": _num(it.get("salt_g"))})
        if not clean:
            continue
        opt = {"title": (str(o.get("title") or "案").strip() or "案")[:20],
               "items": clean,
               "kcal": round(k), "protein_g": round(p, 1),
               "fat_g": round(f, 1), "carb_g": round(c, 1),
               "why": str(o.get("why") or "").strip()[:140]}
        if cap > 0 and opt["kcal"] > cap:
            opt["over_kcal"] = round(opt["kcal"] - float(budget.get("kcal") or 0))
            over.append(opt)
        else:
            ok.append(opt)
    ok.sort(key=lambda x: x["kcal"])
    return ok, over


# ---------- ルールベースの標準案（AIが使えないとき） ----------

_STD_OPTIONS = [
    {"title": "たんぱく質しっかり", "why": "たんぱく質の不足を補いつつ、脂質と食塩を抑えた組み合わせです。",
     "items": [
         {"name": "鶏むね肉（皮なし）焼き", "quantity": "150g", "kcal": 248,
          "protein_g": 34.5, "fat_g": 4.5, "carb_g": 0, "salt_g": 1.0},
         {"name": "ブロッコリー", "quantity": "100g", "kcal": 33,
          "protein_g": 4.3, "fat_g": 0.5, "carb_g": 5.2, "salt_g": 0},
         {"name": "味噌汁（豆腐・わかめ）", "quantity": "1杯", "kcal": 60,
          "protein_g": 4.0, "fat_g": 2.5, "carb_g": 5.0, "salt_g": 1.5},
     ]},
    {"title": "低カロリー和食", "why": "カロリーを抑えたい日に向く、消化のよい和食の組み合わせです。",
     "items": [
         {"name": "木綿豆腐", "quantity": "150g", "kcal": 110,
          "protein_g": 10.5, "fat_g": 6.8, "carb_g": 2.3, "salt_g": 0},
         {"name": "納豆", "quantity": "1パック", "kcal": 90,
          "protein_g": 8.0, "fat_g": 5.0, "carb_g": 5.5, "salt_g": 0.6},
         {"name": "わかめと豆腐のスープ", "quantity": "1杯", "kcal": 45,
          "protein_g": 3.5, "fat_g": 1.5, "carb_g": 4.0, "salt_g": 1.2},
     ]},
    {"title": "間食・軽め", "why": "間食として補う場合の目安です。空腹感を抑えつつ糖質を調整できます。",
     "items": [
         {"name": "無糖ギリシャヨーグルト", "quantity": "100g", "kcal": 62,
          "protein_g": 10.0, "fat_g": 0.4, "carb_g": 3.9, "salt_g": 0.1},
         {"name": "バナナ", "quantity": "1本(100g)", "kcal": 86,
          "protein_g": 1.1, "fat_g": 0.2, "carb_g": 22.5, "salt_g": 0},
     ]},
]


def _fallback_options(ctx: dict):
    ok, _ = _recalc_options(_STD_OPTIONS, {"kcal": float(ctx.get("budget_kcal") or 0)})
    if ok:
        return ok
    # 予算が極端に小さい場合でも最低1案は返す（量の目安として）
    opt = dict(_STD_OPTIONS[2])
    items = []
    k = p = f = c = 0.0
    for it in opt["items"]:
        items.append(dict(it))
        k += it["kcal"]
        p += it["protein_g"]
        f += it["fat_g"]
        c += it["carb_g"]
    return [{"title": opt["title"], "items": items, "kcal": round(k),
             "protein_g": round(p, 1), "fat_g": round(f, 1),
             "carb_g": round(c, 1), "why": opt["why"]}]


# ---------- 応答の組み立て ----------

def _format(ctx: dict, intro: str, options: list, notes: str,
            used_ai: bool, over_count: int = 0) -> str:
    rem = float(ctx.get("remaining_net") or 0)
    base = f"目標 {float(ctx.get('target') or 0):.0f} − 摂取 {float(ctx.get('intake') or 0):.0f}"
    if ctx.get("burn"):
        base += f" ＋ 消費 {float(ctx['burn']):.0f}"
    lines = ["🍽 あと食べていい量（記録から計算）"]
    if rem >= 0:
        lines.append(f"残り {rem:.0f} kcal（{base}）")
    else:
        lines.append(f"⚠ 今日は目標を {abs(rem):.0f} kcal 超えています（{base}）")
    rp = ctx.get("remaining_pfc") or {}
    parts = []
    for label, key in (("たんぱく質", "protein_g"), ("脂質", "fat_g"),
                       ("炭水化物", "carb_g")):
        if rp.get(key) is not None:
            parts.append(f"{label} 残り {float(rp[key]):.0f}g")
    if parts:
        lines.append(" / ".join(parts))
    if ctx.get("avg_intake"):
        lines.append(f"（直近2週間の平均摂取 {float(ctx['avg_intake']):.0f} kcal/日）")

    if intro:
        lines += ["", intro]

    for i, o in enumerate(options[:3], 1):
        lines.append("")
        lines.append(f"【{i}】{o['title']} 約{o['kcal']}kcal"
                     f"（P{o['protein_g']:.0f} F{o['fat_g']:.0f} C{o['carb_g']:.0f}）")
        for it in o["items"]:
            q = f" {it['quantity']}" if it["quantity"] else ""
            lines.append(f"・{it['name']}{q} {it['kcal']:.0f}kcal")
        if o.get("why"):
            lines.append(f"→ {o['why']}")

    if notes:
        lines += ["", notes]
    if over_count:
        lines.append(f"（予算を超える案 {over_count}件 は除外しました）")
    if not used_ai:
        lines.append("（AIに接続できなかったため、標準案を表示しています）")
    lines += ["", "※数値は目安です。食べたら『昼 鶏むね肉 250kcal』のように記録してください。"]
    text = "\n".join(lines)
    return text[:1400]


def propose(user_id: str, user_text: str, extra: dict | None = None):
    """相談への回答（TextSendMessage）。判定不能なら None を返し通常フローに任せる.

    extra: {"considered": 検討中のメニュー, "constraint": 追加要望,
            "budget_ratio": 予算の上限を残りの何割にするか}
    """
    try:
        ctx = build_consult_context(user_id)
    except Exception:
        logger.exception("consult context failed")
        return None

    allow = float(ctx.get("remaining_net") or 0)
    hard = allow if allow >= _MIN_BUDGET else _LIGHT_CAP
    if extra:
        for k in ("considered", "constraint"):
            if extra.get(k):
                ctx[k] = extra[k]
        ratio = _num(extra.get("budget_ratio"))
        if ratio:
            hard = max(hard * float(ratio), _LIGHT_CAP)
    ctx["budget_kcal"] = hard

    data = None
    try:
        data = _call_llm(user_text, ctx, _persona(user_id))
    except Exception:
        logger.warning("consult llm failed", exc_info=True)

    options, over = _recalc_options((data or {}).get("options"), {"kcal": hard})
    intro = str((data or {}).get("intro") or "").strip()
    notes = str((data or {}).get("notes") or "").strip()
    used_ai = bool(data)

    if not options:
        options = _fallback_options(ctx)
        if allow < _MIN_BUDGET:
            notes = (notes + " " if notes else "") + \
                "今日はすでに目標に近い/超えているので、軽めの案だけにしています。"

    from app.handlers.button_builder import qr, msq
    msg = TextSendMessage(text=_format(ctx, intro, options, notes, used_ai,
                                       over_count=len(over)))
    msg.quick_reply = qr(
        msq("もっと軽めに", "もっと軽めに"),
        msq("間食で", "間食でおすすめは？"),
        msq("別の案", "別の案"),
    )
    return msg


def _persona(user_id: str):
    try:
        from app.services.db import load_user_persona
        return load_user_persona(user_id)
    except Exception:
        return None


def _call_llm(user_text: str, ctx: dict, persona):
    """テストから差し替えられるように薄く包む."""
    from app.services.llm_consult import consult_advice
    return consult_advice(user_text, ctx, persona)


# ================= 提案の評価（「これ食べたらどう？」） =================

# 提案した内容と、その場で出した調整案を覚えておく（「記録する」用）
_last_proposal: dict = {}
_PROPOSAL_TTL = 30 * 60

_SLOT_JP = {"breakfast": "朝食", "lunch": "昼食", "dinner": "夕食", "snack": "間食"}

_KCAL_OVER = 50.0       # これ以上超えたら「オーバー」
_KCAL_JUST = 100.0      # この範囲の下振れは「ちょうどいい」
_PROTEIN_SHORT = 15.0   # これ以上足りなければ「たんぱく質不足」
_FAT_OVER = 10.0
_SALT_OVER = 1.0


def _totals(foods: list) -> dict:
    t = {"kcal": 0.0, "protein_g": 0.0, "fat_g": 0.0, "carb_g": 0.0, "salt_g": 0.0}
    for f in foods:
        for k in t:
            v = f.get(k)
            if isinstance(v, (int, float)):
                t[k] += float(v)
    return {k: round(v, 1) for k, v in t.items()}


def judge_proposal(ctx: dict, foods: list) -> dict:
    """提案の合計と、目標に対する過不足をすべてコードで判定する."""
    tot = _totals(foods)
    rem = float(ctx.get("remaining_net") or 0)
    target = float(ctx.get("target") or 0)
    intake = float(ctx.get("intake") or 0)
    diff = round(tot["kcal"] - rem, 1)
    if diff > _KCAL_OVER:
        level = "over"
    elif diff > -_KCAL_JUST:
        level = "just"
    else:
        level = "under"

    pfc_t = ctx.get("pfc_target") or {}
    now = ctx.get("pfc_now") or {}
    pro_t = _num(pfc_t.get("protein_g"))
    if pro_t is None and ctx.get("weight"):
        pro_t = round(float(ctx["weight"]) * 1.6, 1)   # 体重×1.6g の目安
    pro_short = None
    if pro_t is not None:
        pro_short = round(pro_t - (float(now.get("protein_g") or 0) + tot["protein_g"]), 1)

    fat_t = _num(pfc_t.get("fat_g"))
    fat_short = None
    if fat_t is not None:
        fat_short = round(fat_t - (float(now.get("fat_g") or 0) + tot["fat_g"]), 1)

    salt_rem = _num(ctx.get("salt_remaining"))
    salt_over = None
    if salt_rem is not None:
        salt_over = round(tot["salt_g"] - salt_rem, 1)

    flags = []
    if level == "over":
        flags.append(f"カロリー {abs(diff):.0f}kcal オーバー")
    if pro_short is not None and pro_short > _PROTEIN_SHORT:
        flags.append(f"たんぱく質 {pro_short:.0f}g 不足")
    if fat_short is not None and fat_short < -_FAT_OVER:
        flags.append(f"脂質 {abs(fat_short):.0f}g オーバー")
    if salt_over is not None and salt_over > _SALT_OVER:
        flags.append(f"食塩 {salt_over:.1f}g オーバー")

    return {"totals": tot, "remaining": rem, "target": target, "intake": intake,
            "after_intake": round(intake + tot["kcal"], 1), "diff": diff,
            "kcal_level": level, "protein_target": pro_t, "protein_short": pro_short,
            "fat_target": fat_t, "fat_short": fat_short, "salt_over": salt_over,
            "flags": flags}


# --- テストから差し替えられる薄い包み ---

def _extract(user_text: str) -> dict:
    from app.services.llm_consult import extract_proposal_foods
    return extract_proposal_foods(user_text)


def _estimate(foods: list) -> list:
    """既存の栄養推定（llm.estimate_foods_batch / estimate_food_single）を再利用する."""
    from app.services.llm import estimate_foods_batch, estimate_food_single
    items = [{"food_name": f.get("name"),
              "quantity_g": f.get("quantity_g") or f.get("quantity") or None,
              "meal_slot": "snack", "kcal": None} for f in foods]
    try:
        estimate_foods_batch(items)
    except Exception:
        logger.info("batch estimate failed → per-item", exc_info=True)
        for it in items:
            if (it.get("kcal") or 0) > 0:
                continue
            try:
                estimate_food_single(it)
            except Exception:
                logger.warning("estimate failed: %s", it.get("food_name"))
    return [it for it in items if (it.get("kcal") or 0) > 0]


def _phrase(user_text: str, ctx: dict, verdict: dict, persona):
    from app.services.llm_consult import phrase_proposal
    return phrase_proposal(user_text, ctx, verdict, persona)


def _format_proposal(names: str, v: dict, data: dict, used_ai: bool) -> str:
    t = v["totals"]
    lines = [f"🍽 「{names}」を食べたら？（今日の記録から計算）", ""]
    lines.append(f"合計 約{t['kcal']:.0f}kcal（P{t['protein_g']:.0f} "
                 f"F{t['fat_g']:.0f} C{t['carb_g']:.0f} 食塩{t['salt_g']:.1f}g）")
    lines.append(f"今日の摂取 {v['intake']:.0f}kcal → 足すと {v['after_intake']:.0f}kcal"
                 f"（目標 {v['target']:.0f}kcal）")
    if v["kcal_level"] == "over":
        lines.append(f"⚠ 残り {v['remaining']:.0f}kcal に対して {v['diff']:.0f}kcal オーバー")
    elif v["kcal_level"] == "just":
        lines.append(f"✅ 残り {v['remaining']:.0f}kcal にほぼぴったりです")
    else:
        lines.append(f"✅ 残り {v['remaining']:.0f}kcal のうち {t['kcal']:.0f}kcal を使います"
                     f"（{abs(v['diff']):.0f}kcal 余ります）")
    if v["protein_short"] is not None:
        if v["protein_short"] > _PROTEIN_SHORT:
            lines.append(f"⚠ たんぱく質が {v['protein_short']:.0f}g 足りません"
                         f"（目標 {v['protein_target']:.0f}g）")
        elif v["protein_short"] < -_PROTEIN_SHORT:
            lines.append(f"（たんぱく質は目標を {abs(v['protein_short']):.0f}g 超えます）")
        else:
            lines.append("✅ たんぱく質は目標にほぼ届きます")
    else:
        lines.append("（目標が未設定のため、カロリーと一般的な目安で判定しています）")
    if v["fat_short"] is not None and v["fat_short"] < -_FAT_OVER:
        lines.append(f"⚠ 脂質が {abs(v['fat_short']):.0f}g オーバーします")
    if v["salt_over"] is not None and v["salt_over"] > _SALT_OVER:
        lines.append(f"⚠ 食塩が {v['salt_over']:.1f}g オーバーします")

    verdict = str((data or {}).get("verdict") or "").strip()
    if verdict:
        lines += ["", verdict]
    adjs = [a for a in ((data or {}).get("adjustments") or []) if isinstance(a, dict)]
    if adjs:
        lines += ["", "【調整するなら】"]
        for a in adjs[:2]:
            label = str(a.get("label") or "").strip()[:40]
            detail = str(a.get("detail") or "").strip()[:80]
            sk = a.get("save_kcal")
            tail = ""
            if isinstance(sk, (int, float)) and sk:
                tail = f"（約{abs(float(sk)):.0f}kcal 減）"
            lines.append(f"・{label}{tail}" + (f" {detail}" if detail else ""))
    notes = str((data or {}).get("notes") or "").strip()
    if notes:
        lines += ["", notes]
    if not used_ai:
        lines.append("（AIに接続できなかったため、計算結果のみ表示しています）")
    lines += ["", "※数値は目安です。「記録する」で今日の記録に追加できます。"]
    return "\n".join(lines)[:1500]


def evaluate_proposal(user_id: str, user_text: str):
    """「これ食べたらどう？」に、カロリー/PFCの過不足を計算して答える.

    数値の計算はすべてコード。AIは品目の抽出と文言だけを担当する。
    既に食べた報告（過去形）と判定した場合は None を返し、記録フローに任せる。
    """
    try:
        ctx = build_consult_context(user_id)
    except Exception:
        logger.exception("proposal context failed")
        return None
    try:
        ex = _extract(user_text) or {}
    except Exception:
        logger.warning("proposal extract failed", exc_info=True)
        ex = {}
    if ex.get("ate_already"):
        return None                       # 食べた報告は記録フローへ
    names_in = [f for f in (ex.get("foods") or [])
                if isinstance(f, dict) and str(f.get("name") or "").strip()]
    if not names_in:
        return TextSendMessage(text=(
            "食べるものを読み取れませんでした。\n"
            "食品名を入れて送ってください。\n"
            "例: 「ラーメン食べたらどう？」「唐揚げ5個はアリ？」"))
    foods = _estimate(names_in)
    if not foods:
        return TextSendMessage(text=(
            "⚠ カロリーを推定できませんでした。\n"
            "食品名と量（例: 「ラーメン1杯」）をもう一度送ってください"))

    v = judge_proposal(ctx, foods)
    label = "＋".join(str(f.get("food_name")) for f in foods)[:40]
    data = None
    try:
        data = _phrase(user_text, ctx, v, _persona(user_id))
    except Exception:
        logger.warning("proposal phrase failed", exc_info=True)

    msg = TextSendMessage(text=_format_proposal(label, v, data, bool(data)))
    try:
        from app.handlers.button_builder import qr, msq
        msg.quick_reply = qr(msq("記録する", "記録する"),
                             msq("もっと軽く", "もっと軽く"),
                             msq("やめとく", "やめとく"))
    except Exception:
        logger.warning("proposal quick_reply failed", exc_info=True)

    _last_proposal[user_id] = {"foods": foods, "totals": v["totals"],
                               "label": label, "at": time.time(),
                               "verdict": v, "text": user_text}
    return msg


def _get_last(user_id: str):
    p = _last_proposal.get(user_id)
    if not p:
        return None
    if time.time() - float(p.get("at") or 0) > _PROPOSAL_TTL:
        _last_proposal.pop(user_id, None)
        return None
    return p


def record_last(user_id: str):
    """提案した内容をそのまま記録する（確認画面を出して確定させる）."""
    from app.handlers.text_handler import _pending
    p = _get_last(user_id)
    if not p or user_id in _pending:
        return None
    slot = _guess_slot()
    foods = []
    for f in p["foods"]:
        foods.append({"name": f.get("food_name") or "未名",
                      "kcal": float(f.get("kcal") or 0),
                      "protein_g": f.get("protein_g"), "fat_g": f.get("fat_g"),
                      "carb_g": f.get("carb_g"), "salt_g": f.get("salt_g"),
                      "quantity_g": f.get("quantity_g"),
                      "_source_type": "llm_estimate", "_confidence": "estimated"})
    _pending[user_id] = {"foods": foods, "meal_slot": slot, "date": today_jst(),
                         "awaiting_slot": False, "created_at": time.time()}
    _last_proposal.pop(user_id, None)
    total = sum(f["kcal"] for f in foods)
    names = "、".join(f["name"] for f in foods)
    msg = TextSendMessage(
        text=(f"{_SLOT_JP.get(slot, slot)}として記録します。\n{names}\n"
              f"合計 約{total:.0f} kcal\n\nこの内容で記録しますか？"))
    from app.handlers.button_builder import qr, pb
    msg.quick_reply = qr(pb("✅ 記録する", "cmd=confirm_yes", "記録する"),
                         pb("キャンセル", "cmd=confirm_no", "キャンセル"))
    return msg


def _guess_slot() -> str:
    from app.services.dates import now_jst
    h = now_jst().hour
    if h < 10:
        return "breakfast"
    if h < 15:
        return "lunch"
    if h < 21:
        return "dinner"
    return "snack"


def lighten_last(user_id: str):
    """提案した内容を踏まえて、もっと軽い献立を出し直す."""
    p = _get_last(user_id)
    if not p:
        return None
    return propose(user_id, "さっき検討した内容でもっと軽くしたい",
                   extra={"considered": p.get("label") or "",
                          "constraint": "もっと軽め（残り予算の半分以下）で、"
                                        "同じものを食べたい気持ちを活かした案",
                          "budget_ratio": 0.5})


def handle_followup(user_id: str, text: str):
    """提案直後の「記録する／もっと軽く／やめとく」を処理する."""
    t = (text or "").strip()
    if t in ("やめとく", "やっぱやめる"):
        _last_proposal.pop(user_id, None)
        return TextSendMessage(text="了解です。食べたら『ラーメン食べた』のように送ってください")
    if t == "記録する":
        return record_last(user_id)
    if t in ("もっと軽く", "軽くする"):
        return lighten_last(user_id)
    return None
