"""ルールベースの「ひとこと」生成（AI失敗時のフォールバック専用）。"""
from __future__ import annotations

FAT_KCAL_PER_KG = 7200
FAT_NOTE = "（体脂肪1kg≒約7,000〜7,200kcal換算の目安）"

_TITLES = {
    "daily": "今日のひとこと",
    "weekly": "今週のひとこと",
    "monthly": "今月のひとこと",
}


def _num(s) -> int:
    try:
        return int(round(float(s)))
    except (TypeError, ValueError):
        return 0


def fat_equiv(kcal: float) -> str:
    if not kcal or kcal <= 0:
        return ""
    kg = kcal / FAT_KCAL_PER_KG
    if kg < 0.05:
        return ""
    return f"体脂肪 約{kg:.2f}kg分" if kg < 1 else f"体脂肪 約{kg:.1f}kg分"


def _pct(v, target):
    try:
        return float(v) / float(target) * 100 if target else None
    except (TypeError, ValueError):
        return None


def _daily(intake, target, burn, deficit) -> list:
    lines, r = [], _pct(intake, target)
    if r is not None:
        if r < 70:
            lines.append(f"⚠️ 摂取が目標の{int(r)}%と少なめ。たんぱく質を1品足そう。")
        elif r <= 110:
            lines.append(f"✅ 摂取は目標の{int(r)}%。いいペース。")
        elif r <= 120:
            lines.append(f"🍚 やや食べすぎ（目標の{int(r)}%）。夜だけ軽くすると調整できる。")
        else:
            lines.append(f"🍚 今日は食べすぎ（目標の{int(r)}%）。明日の朝を軽めに。")
    if burn and _num(burn) >= 500:
        lines.append(f"🔥 たくさん運動したね！{_num(burn)}kcal消費。")
    elif burn and _num(burn) >= 200:
        lines.append(f"🔥 {_num(burn)}kcal消費、いい動き。")
    fe = fat_equiv(deficit)
    if fe:
        lines.append(f"📉 赤字{_num(deficit)}kcal ≒ {fe}{FAT_NOTE}")
    return lines


def _weekly(avg: float, target, total_burn, logged_days, weight_delta) -> list:
    lines, r = [], _pct(avg, target)
    if logged_days >= 7:
        lines.append("📒 7/7日、全部記録できたね。継続力すごい。")
    elif logged_days >= 5:
        lines.append(f"📒 記録は{logged_days}/7日。まずまず。")
    else:
        lines.append(f"📒 記録は{logged_days}/7日。1日1回から続けよう。")
    if total_burn and _num(total_burn) >= 1500:
        lines.append(f"🔥 今週の運動は合計{_num(total_burn)}kcal。よく動いた一週間。")
    if r is not None and r > 115:
        lines.append(f"📈 週平均の摂取は目標の{int(r)}%で、やや食べすぎ傾向。")
    elif r is not None and r < 75:
        lines.append(f"📉 週平均の摂取は目標の{int(r)}%と少なめ。筋肉が落ちるので少し戻そう。")
    if weight_delta is not None:
        if weight_delta <= -1.0:
            lines.append(f"⚠️ 体重が{abs(weight_delta):.1f}kg減。週1kg超は急すぎるので食事量を戻そう。")
        elif weight_delta <= -0.3:
            lines.append(f"⚖️ 体重は{abs(weight_delta):.1f}kg減。ちょうどいいペース。")
    return lines


def _monthly(deficit, avg, target, logged_days, elapsed_days, weight_delta) -> list:
    lines, r = [], _pct(avg, target)
    rate = (logged_days / elapsed_days * 100) if elapsed_days else 0
    fe = fat_equiv(deficit)
    if fe:
        lines.append(f"🎉 今月の赤字合計は{_num(deficit)}kcal ≒ {fe}{FAT_NOTE}")
    if rate >= 80:
        lines.append(f"📒 記録率{int(rate)}%。もう生活の一部だね。")
    elif rate < 50:
        lines.append(f"📒 記録率{int(rate)}%。来月は「夜だけ送る」から始めよう。")
    if r is not None and r > 110:
        lines.append(f"📈 月平均で目標の{int(r)}%。外食が多い時期かも。")
    if weight_delta is not None:
        if _pct(avg, target) is not None and r is not None and r < 75:
            lines.append("⚠️ 減量ペースが速すぎます。体調不良や筋力低下のリスクがあるので摂取を戻して。")
        elif -4.0 <= weight_delta <= -1.0:
            lines.append(f"⚖️ 1ヶ月で{abs(weight_delta):.1f}kg減。健康的なペース。")
        elif abs(weight_delta) < 1.0:
            lines.append("⚖️ 横ばい。維持も立派な成果。")
    return lines


def _kd(facts: dict, *keys):
    """日本語キーの facts から数値・文字列を取り出すヘルパー。"""
    out = []
    for k in keys:
        v = facts.get(k)
        if isinstance(v, str):
            v = v.replace("kcal", "").replace("kg", "").replace("%", "").split("（")[0].strip()
        out.append(v)
    return out


def ai_fallback(scope: str, facts: dict) -> dict:
    """AI 生成失敗時の代替文（3行構成に整形して返す）。"""
    try:
        if scope == "daily":
            intake, target, burn, deficit = _kd(facts, "摂取kcal", "目標摂取kcal", "運動消費kcal", "赤字kcal")
            lines = _daily(intake, target, burn, deficit)
        elif scope == "weekly":
            avg = facts.get("記録日あたりの平均摂取kcal", "0")
            target = facts.get("目標摂取kcal") or 0
            burn = facts.get("週間の運動消費kcal", 0)
            logged = int(str(facts.get("記録日数", "0")).split("日")[0] or 0)
            wd_raw = facts.get("体重変化", "未記録")
            wd = None if wd_raw == "未記録" else float(str(wd_raw).replace("kg", ""))
            lines = _weekly(float(str(avg).replace("kcal", "") or 0), target, burn, logged, wd)
        else:
            deficit = facts.get("月間の赤字合計kcal", 0)
            avg = float(str(facts.get("記録日あたりの平均摂取kcal", "0")).replace("kcal", "") or 0)
            target = facts.get("目標摂取kcal") or 0
            logged = int(str(facts.get("記録日数", "0")).split("日")[0] or 0)
            elapsed = int(str(facts.get("経過日数", "0")).split("日")[0] or 0)
            wd_raw = facts.get("体重変化", "未記録")
            wd = None if wd_raw == "未記録" else float(str(wd_raw).replace("kg", "").split("（")[0])
            lines = _monthly(deficit, avg, target, logged, elapsed, wd)
    except Exception:  # noqa: BLE001
        lines = ["集計を表示しています。"]

    lines = [l for l in lines if l]
    return {
        "headline": _TITLES.get(scope, "まとめ"),
        "comment": " ".join(lines[:2]) if lines else "記録を続けよう。",
        "advice": lines[2] if len(lines) > 2 else "明日も1回だけ送ってみよう。",
    }
Copy
