"""Flex Message の組み立て."""
from typing import List, Dict, Any, Optional

COLOR_INTAKE = "#FFB86C"   # 摂取 = 橙
COLOR_BURN = "#6FA8DC"     # 消費 = 青
COLOR_TARGET = "#9CCC65"   # 目標 = 緑
COLOR_LANE = "#F0F0F0"     # 背景レーン
BAR_MAX_PX = 200


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
                _row("摂取", f"{intake:.0f} kcal"),
                _row("消費", f"{burn:.0f} kcal"),
                _row("収支(消費−摂取)", f"{deficit:+.0f} kcal",
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
                    _row("摂取", f"{r['intake_kcal']:.0f} kcal"),
                    _row("消費", f"{r['consumed_kcal']:.0f} kcal"),
                    _row("収支", f"{r['deficit_kcal']:+.0f} kcal"),
                ],
            },
        })
    return {"type": "carousel", "contents": bubbles}


# ---- 重ね棒 (摂取・消費を1レーンに、少ない方を手前に) ----

def _overlap_bar(intake: float, burn: float):
    """摂取(橙)と消費(青)を1本のレーンに重ねる.

    全長 = 大きい方の値。手前 = 少ない方。残差は大きい方の色で続く。
    LINE Flex は絶対座標が使えないため、横並びboxの連結で表現する。
    """
    bigger = max(intake, burn, 1)
    w_in = max(int(intake / bigger * BAR_MAX_PX), 2)
    w_burn = max(int(burn / bigger * BAR_MAX_PX), 2)

    if intake <= burn:
        # 手前: 摂取(全量) / 奥: 消費の残り
        seg = [
            {"w": w_in, "color": COLOR_INTAKE},
            {"w": max(w_burn - w_in, 0), "color": COLOR_BURN},
        ]
    else:
        # 手前: 消費(全量) / 奥: 摂取の残り
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
            {"type": "text", "text": "(全長=多い方 / 手前=少ない方)",
             "size": "xxs", "color": "#999999"},
        ],
    }


def _target_row(target_kcal: float, max_kcal: float):
    """目標を棒＋数値で1行表示."""
    w = max(int(target_kcal / max(max_kcal, 1) * BAR_MAX_PX), 3)
    return {
        "type": "box", "layout": "vertical", "spacing": "xs",
        "margin": "sm",
        "contents": [
            {"type": "text",
             "text": f"目標 {target_kcal:.0f} kcal",
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
        txt = f"収支 {deficit:+.0f} kcal（1日平均 {avg:+.0f}）"
    else:
        txt = f"収支 {deficit:+.0f} kcal"
    return {"type": "text", "text": txt, "size": "sm",
            "weight": "bold", "margin": "md",
            "color": "#27AE60" if deficit >= 0 else "#E74C3C"}


def _period_bubble(title: str, intake: float, burn: float,
                   target_kcal: Optional[float],
                   days: int = 0, is_total: bool = False):
    """目標 → 摂取/消費(重ね棒) → 収支 の語順で1バブル."""
    max_kcal = max(intake, burn, target_kcal or 0, 1)
    deficit = burn - intake
    contents = [
        {"type": "text", "text": title, "weight": "bold",
         "size": "sm" if not is_total else "md"},
    ]
    if target_kcal:
        contents.append(_target_row(target_kcal, max_kcal))
    contents.append({"type": "text",
                     "text": f"摂取 {intake:.0f} / 消費 {burn:.0f} kcal",
                     "size": "xxs", "color": "#555555",
                     "margin": "sm"})
    contents.append(_overlap_bar(intake, burn))
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

    1枚目 = 期間トータル (目標→摂取/消費 重ね棒→収支/1日平均)
    2枚目以降 = 日別 (同じ形、古い順)
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

    total_in = sum(r["intake_kcal"] for r in rows)
    total_burn = sum(r["consumed_kcal"] for r in rows)
    total_target = (target_kcal or 0) * len(rows)

    bubbles = [_period_bubble(
        f"📊 直近{len(rows)}日トータル",
        total_in, total_burn,
        total_target if target_kcal else None,
        days=len(rows), is_total=True,
    )]
    for r in rows:
        bubbles.append(_period_bubble(
            r["date"], r["intake_kcal"], r["consumed_kcal"], target_kcal,
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
    total_deficit = total_burn - total_intake
    avg_intake = total_intake / len(rows)
    total_target = (target_kcal or 0) * len(rows)
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
        days=len(rows), is_total=True,
    )
    # 補足の数値行を body 末尾に追加
    bubble["body"]["contents"] += [
        {"type": "separator", "margin": "md"},
        _row("記録日数", f"{len(rows)} 日"),
        _row("1日平均摂取", f"{avg_intake:.0f} kcal"),
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
             "text": f"{label} {value:.0f} kcal",
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
