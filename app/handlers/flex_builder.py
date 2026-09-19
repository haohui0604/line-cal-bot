"""Flex Message の組み立て."""
from typing import List, Dict, Any, Optional

COLOR_INTAKE = "#FFB86C"   # 摂取 = 橙
COLOR_BURN = "#6FA8DC"     # 消費 = 青
COLOR_TARGET = "#9CCC65"   # 目標 = 緑
COLOR_LANE = "#F0F0F0"     # 背景レーン
BAR_MAX_PX = 200
HEADROOM = 1.15            # 最大値に15%の余白 → 棒がレーン端に張り付かない


def summary_flex(s: Dict[str, Any]):
    intake = s.get("intake_kcal", 0)
    burn = s.get("burn_kcal", 0)
    deficit = burn - intake
    return {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [{
                "type": "text",
                "text": f"🍱 {s.get('date', '')} のサマリ",
                "weight": "bold",
                "size": "lg",
            }],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                _row("摂取", f"{intake:,.0f} kcal"),
                _row("消費", f"{burn:,.0f} kcal"),
                _row("収支(消費−摂取)", f"{deficit:+,.0f} kcal",
                     color="#27AE60" if deficit >= 0 else "#E74C3C"),
                _row("タンパク質", f"{s.get('protein_g', 0):.0f} g"),
                _row("脂質", f"{s.get('fat_g', 0):.0f} g"),
                _row("炭水化物", f"{s.get('carb_g', 0):.0f} g"),
                _row("食塩", f"{s.get('salt_g', 0):.2f} g"),
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [{
                "type": "text",
                "text": "『集計』『週次』『月次』『目標』『履歴』が使えます",
                "size": "xs",
                "color": "#888888",
            }],
        },
    }


def history_flex(rows: List[Dict[str, Any]]):
    bubbles = []
    for r in rows[:7]:
        bubbles.append({
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {"type": "text", "text": r["date"], "weight": "bold"},
                    _row("摂取", f"{r['intake_kcal']:,.0f} kcal"),
                    _row("消費", f"{r['consumed_kcal']:,.0f} kcal"),
                    _row("収支", f"{r['deficit_kcal']:+,.0f} kcal"),
                ],
            },
        })
    return {"type": "carousel", "contents": bubbles}


# ---- 重ね棒 (摂取・消費を1レーンに、少ない方を手前に) ----

def _overlap_bar(intake: float, burn: float, scale: float):
    """摂取(橙)と消費(青)を1本のレーンに重ねる.

    scale: この期間の共通スケール (最大値×HEADROOM)。
           全バブルで同じ scale を使うので日別の大小が比較できる。
    全長 = max(摂取,消費) / scale。手前 = 少ない方。残差は多い方の色で続く。
    """
    scale = max(scale, 1)
    w_in = min(max(int(intake / scale * BAR_MAX_PX), 2), BAR_MAX_PX)
    w_burn = min(max(int(burn / scale * BAR_MAX_PX), 2), BAR_MAX_PX)

    if intake <= burn:
        seg = [
            {"w": w_in, "color": COLOR_INTAKE},
            {"w": max(w_burn - w_in, 0), "color": COLOR_BURN},
        ]
    else:
        seg = [
            {"w": w_burn, "color": COLOR_BURN},
            {"w": max(w_in - w_burn, 0), "color": COLOR_INTAKE},
        ]
    return {
        "type": "box", "layout": "horizontal",
        "height": "14px", "margin": "xs",
        "contents": [
            {"type": "box", "layout": "vertical",
             "width": f"{s['w']}px", "height": "14px",
             "backgroundColor": s["color"], "contents": []}
            for s in seg if s["w"] > 0
        ],
    }


def _legend():
    return {
        "type": "box", "layout": "horizontal", "spacing": "md",
        "margin": "xs",
        "contents": [
            {"type": "text", "text": "■ 摂取", "size": "xxs",
             "color": COLOR_INTAKE, "weight": "bold"},
            {"type": "text", "text": "■ 消費", "size": "xxs",
             "color": COLOR_BURN, "weight": "bold"},
            {"type": "text", "text": "（手前=少ない方）",
             "size": "xxs", "color": "#999999"},
        ],
    }


def _target_row(target_kcal: float, scale: float):
    """目標を棒＋数値で1行表示."""
    w = min(max(int(target_kcal / max(scale, 1) * BAR_MAX_PX), 3), BAR_MAX_PX)
    return {
        "type": "box", "layout": "vertical", "spacing": "xs",
        "margin": "sm",
        "contents": [
            {"type": "text",
             "text": f"目標 {target_kcal:,.0f} kcal",
             "size": "xxs", "color": "#555555"},
            {"type": "box", "layout": "vertical",
             "width": f"{BAR_MAX_PX}px", "height": "12px",
             "backgroundColor": COLOR_LANE, "cornerRadius": "3px",
             "contents": [
                 {"type": "box", "layout": "vertical",
                  "width": f"{w}px", "height": "12px",
                  "backgroundColor": COLOR_TARGET,
                  "cornerRadius": "3px", "contents": []},
             ]},
        ],
    }


def _deficit_text(deficit: float, per_day: bool = False,
                  days: int = 0):
    if per_day and days:
        avg = deficit / days
        txt = f"収支 {deficit:+,.0f} kcal（1日平均 {avg:+,.0f}）"
    else:
        txt = f"収支 {deficit:+,.0f} kcal"
    return {"type": "text", "text": txt, "size": "sm",
            "weight": "bold", "margin": "md",
            "color": "#27AE60" if deficit >= 0 else "#E74C3C"}


def _period_bubble(title: str, intake: float, burn: float,
                   target_kcal: Optional[float], scale: float,
                   days: int = 0, is_total: bool = False):
    """目標 → 摂取/消費(重ね棒) → 収支 の語順で1バブル."""
    deficit = burn - intake
    contents = [
        {"type": "text", "text": title, "weight": "bold",
         "size": "sm" if not is_total else "md"},
    ]
    if target_kcal:
        contents.append(_target_row(target_kcal, scale))
    # 摂取/消費は2行に分割 (micro バブルでの文字切れ防止)
    contents.append({"type": "text",
                     "text": f"摂取 {intake:,.0f} kcal",
                     "size": "xxs", "color": "#555555",
                     "margin": "sm"})
    contents.append({"type": "text",
                     "text": f"消費 {burn:,.0f} kcal",
                     "size": "xxs", "color": "#555555"})
    contents.append(_overlap_bar(intake, burn, scale))
    contents.append(_legend())
    contents.append(_deficit_text(deficit, per_day=is_total, days=days))
    return {
        "type": "bubble", "size": "micro",
        "body": {"type": "box", "layout": "vertical",
                 "spacing": "sm", "contents": contents},
    }


def weekly_chart_flex(rows: List[Dict[str, Any]], days: int = 7,
                      target_kcal: Optional[float] = None,
                      title: Optional[str] = None):
    """直近 N 日のレポート.

    1枚目 = 期間トータル (トータル目標基準スケール)
    2枚目以降 = 日別 (全日共通スケール → 日ごとの大小が比較できる)
    """
    if not rows:
        return {
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [{"type": "text",
                              "text": "履歴データがありません"}],
            },
        }
    rows = rows[:days][::-1]  # 古い順

    # 日別バブル共通スケール (余白込み) → カンスト回避 + 日間比較が可能
    day_scale = max(
        [max(r["intake_kcal"], r["consumed_kcal"], target_kcal or 0)
         for r in rows] + [1]
    ) * HEADROOM

    total_in = sum(r["intake_kcal"] for r in rows)
    total_burn = sum(r["consumed_kcal"] for r in rows)
    total_target = (target_kcal or 0) * len(rows)
    total_scale = max(total_in, total_burn, total_target, 1) * HEADROOM

    bubbles = [_period_bubble(
        f"📊 直近{len(rows)}日トータル",
        total_in, total_burn,
        total_target if target_kcal else None,
        total_scale,
        days=len(rows), is_total=True,
    )]
    for r in rows:
        bubbles.append(_period_bubble(
            r["date"], r["intake_kcal"], r["consumed_kcal"],
            target_kcal, day_scale,
        ))
    return {"type": "carousel", "contents": bubbles}


def monthly_summary_flex(rows: List[Dict[str, Any]],
                         target_kcal: Optional[float] = None):
    """月次: トータル1バブル (週次と同じ形)."""
    if not rows:
        return {
            "type": "bubble",
            "body": {"type": "box", "layout": "vertical",
                     "contents": [{"type": "text",
                                   "text": "履歴データがありません"}]},
        }
    total_intake = sum(r["intake_kcal"] for r in rows)
    total_burn = sum(r["consumed_kcal"] for r in rows)
    total_target = (target_kcal or 0) * len(rows)
    total_scale = max(total_intake, total_burn, total_target, 1) * HEADROOM
    avg_intake = total_intake / len(rows)
    days_over = sum(
        1 for r in rows
        if target_kcal and r["intake_kcal"] > target_kcal
    )
    achieve_rate = (
        (len(rows) - days_over) / len(rows) * 100 if rows else 0
    )

    bubble = _period_bubble(
        f"📅 直近{len(rows)}日トータル",
        total_intake, total_burn,
        total_target if target_kcal else None,
        total_scale,
        days=len(rows), is_total=True,
    )
    bubble["body"]["contents"] += [
        {"type": "separator", "margin": "md"},
        _row("記録日数", f"{len(rows)} 日"),
        _row("1日平均摂取", f"{avg_intake:,.0f} kcal"),
        _row("目標達成率(目標以下の日)", f"{achieve_rate:.0f} %",
             color="#27AE60" if achieve_rate >= 80 else "#E67E22"),
    ]
    return bubble


def _bar_row(label: str, value: float, bar_width: int, color: str):
    """棒1行 (旧来の単独棒。互換のため残置)."""
    return {
        "type": "box", "layout": "vertical", "spacing": "xs",
        "margin": "sm",
        "contents": [
            {"type": "text",
             "text": f"{label} {value:,.0f} kcal",
             "size": "xxs", "color": "#555555"},
            {"type": "box", "layout": "vertical",
             "width": f"{BAR_MAX_PX}px", "height": "12px",
             "backgroundColor": COLOR_LANE, "cornerRadius": "3px",
             "contents": [
                 {"type": "box", "layout": "vertical",
                  "width": f"{bar_width}px", "height": "12px",
                  "backgroundColor": color, "cornerRadius": "3px",
                  "contents": []},
             ]},
        ],
    }


def _row(label: str, value: str, color: str = "#333333"):
    return {
        "type": "box",
        "layout": "horizontal",
        "contents": [
            {"type": "text", "text": label, "color": "#555555",
             "size": "sm"},
            {"type": "text", "text": value, "align": "end",
             "color": color, "size": "sm", "weight": "bold"},
        ],
    }

# ==================== 今日のレポート＋AIコメント ====================

SLOT_LABELS = {"breakfast": "朝食", "lunch": "昼食",
               "dinner": "夕食", "snack": "間食"}


def _ai_comment_box(result):
    """AIコメント（見出し＋本文＋アドバイス）をFlexブロックに整形。"""
    if not result:
        return None
    return {
        "type": "box", "layout": "vertical", "margin": "lg", "spacing": "sm",
        "backgroundColor": "#F7F7F9", "cornerRadius": "8px", "paddingAll": "12px",
        "contents": [
            {"type": "text", "text": result["headline"], "weight": "bold",
             "size": "sm", "color": "#333333", "wrap": True},
            {"type": "text", "text": result["comment"], "size": "sm",
             "color": "#555555", "wrap": True, "margin": "sm"},
            {"type": "text", "text": "👉 " + result["advice"], "size": "xs",
             "color": "#8A6D3B", "wrap": True, "margin": "sm"},
        ],
    }


def _slot_row(label, kcal, scale):
    """スロット名＋kcal＋細棒の1行。既存の BAR_MAX_PX / COLOR_INTAKE を前提。"""
    w = min(max(int(kcal / scale * BAR_MAX_PX), 0), BAR_MAX_PX) if scale else 0
    bar = [{"type": "box", "layout": "vertical", "width": f"{w}px",
            "height": "6px", "backgroundColor": COLOR_INTAKE,
            "cornerRadius": "3px", "contents": []}] if w else []
    return {"type": "box", "layout": "vertical", "margin": "sm", "contents": [
        {"type": "box", "layout": "baseline", "contents": [
            {"type": "text", "text": label, "size": "xs", "color": "#333333", "flex": 0},
            {"type": "text", "text": f"{kcal:,.0f} kcal", "size": "xxs",
             "color": "#8a8a8a", "align": "end"}]},
        {"type": "box", "layout": "vertical", "height": "6px",
         "backgroundColor": "#eeeeee", "cornerRadius": "3px", "contents": bar},
    ]}


def daily_flex(date_label, summary, meals, burn, target, comment):
    """今日のレポート。目標 → 朝食/昼食/夕食/間食 → 合計と消費の重ね棒 → 収支 → AIコメント。"""
    total = summary["kcal"]
    slot_kcals = [meals.get(k, {}).get("kcal", 0) for k in SLOT_LABELS]
    peak = max([total, burn or 0, target or 0, *slot_kcals, 1])
    scale = peak * 1.15   # 15%の余白（カンスト防止）

    body = [
        {"type": "text", "text": f"今日のレポート（{date_label}）",
         "weight": "bold", "size": "md"},
    ]
    if target:
        body.append(_target_row(target, scale))   # 既存の目標行ヘルパー
    for key, label in SLOT_LABELS.items():
        body.append(_slot_row(label, meals.get(key, {}).get("kcal", 0), scale))
    body.append({"type": "separator", "margin": "md"})
    body.append({"type": "text", "text": f"合計 {total:,.0f} kcal",
                 "size": "xxs", "color": "#555555", "margin": "sm"})
    body.append({"type": "text", "text": f"消費 {burn:,.0f} kcal",
                 "size": "xxs", "color": "#555555"})
    body.append(_overlap_bar(total, burn, scale))   # 既存の重ね棒
    body.append(_legend())                          # 既存の凡例
    body.append(_deficit_text(burn - total, per_day=False, days=1))
    box = _ai_comment_box(comment)
    if box:
        body.append(box)

    return {"type": "bubble", "body": {"type": "box", "layout": "vertical",
            "spacing": "sm", "contents": body}}
