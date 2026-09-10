"""Flex Message の組み立て."""
from typing import List, Dict, Any, Optional


def summary_flex(s: Dict[str, Any]):
    intake = s.get("intake_kcal", 0)
    burn = s.get("burn_kcal", 0)
    deficit = burn - intake
    body = {
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
                _row("赤字", f"{deficit:+.0f} kcal",
                     color="#E74C3C" if deficit < 0 else "#27AE60"),
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
                "text": "『集計』『週次』『月次』『目標 1800』『修正 9/9 昼 牛丼 700』が使えます",
                "size": "xs",
                "color": "#888888",
            }],
        },
    }
    return body


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
                    _row("赤字", f"{r['deficit_kcal']:+.0f} kcal"),
                ],
            },
        })
    return {"type": "carousel", "contents": bubbles}


def weekly_chart_flex(rows: List[Dict[str, Any]], days: int = 7,
                      target_kcal: Optional[float] = None,
                      title: Optional[str] = None):
    """直近 N 日を colored-box 棒グラフで可視化する Flex カルーセル.

    LINE の ImageMessage は HTTPS 公開 URL が必須で、Render 無料枠には
    永続ホスティングが無いため、Flex の backgroundColor を持つ box を
    擬似棒として使用する。
    """
    if not rows:
        return {
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [{"type": "text", "text": "履歴データがありません"}],
            },
        }
    rows = rows[:days][::-1]  # 古い順に並べ替え
    max_kcal = max(
        max(r["intake_kcal"], r["consumed_kcal"], target_kcal or 0, 1)
        for r in rows
    )
    header = title or f"📊 直近{days}日レポート"

    bubbles = []
    for r in rows:
        intake_w = max(int(r["intake_kcal"] / max_kcal * 250), 5)
        burn_w   = max(int(r["consumed_kcal"] / max_kcal * 250), 5)
        target_w = (max(int((target_kcal or 0) / max_kcal * 250), 5)
                    if target_kcal else 0)

        bars = [
            _bar_row("摂取", r["intake_kcal"], f"{intake_w}px", "#FFB86C"),
            _bar_row("消費", r["consumed_kcal"], f"{burn_w}px", "#6FA8DC"),
        ]
        if target_kcal:
            bars.append(_bar_row("目標", target_kcal,
                                 f"{target_w}px", "#9CCC65"))

        contents = [
            {"type": "text", "text": r["date"],
             "weight": "bold", "size": "md"},
            {"type": "box", "layout": "vertical", "spacing": "md",
             "margin": "md", "contents": bars + [
                {"type": "text",
                 "text": f"赤字 {r['deficit_kcal']:+.0f} kcal",
                 "size": "sm", "weight": "bold", "margin": "lg",
                 "color": "#E74C3C" if r["deficit_kcal"] < 0 else "#27AE60"},
             ]},
        ]
        bubbles.append({
            "type": "bubble", "size": "micro",
            "body": {"type": "box", "layout": "vertical", "contents": contents},
        })

    carousel = {"type": "carousel", "contents": bubbles}
    if title:
        carousel = {
            "type": "bubble",
            "size": "giga",
            "header": {
                "type": "box", "layout": "vertical",
                "contents": [{"type": "text", "text": title,
                              "weight": "bold", "size": "md"}],
            },
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [{"type": "text",
                              "text": "カルーセルで日別に確認できます"}],
            },
        }
    return carousel


def monthly_summary_flex(rows: List[Dict[str, Any]],
                         target_kcal: Optional[float] = None):
    """月次レポート用: 合計/平均/最大/最小/達成率のサマリーバブル."""
    if not rows:
        return {
            "type": "bubble",
            "body": {"type": "box", "layout": "vertical",
                     "contents": [{"type": "text",
                                   "text": "履歴データがありません"}]},
        }
    total_intake = sum(r["intake_kcal"] for r in rows)
    total_burn   = sum(r["consumed_kcal"] for r in rows)
    total_deficit = total_burn - total_intake
    avg_intake = total_intake / len(rows)
    days_over = sum(
        1 for r in rows
        if target_kcal and r["intake_kcal"] > target_kcal
    )
    achieve_rate = (
        (len(rows) - days_over) / len(rows) * 100 if rows else 0
    )
    body = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "contents": [{"type": "text", "text": "📅 月次レポート",
                          "weight": "bold", "size": "lg"}],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                _row("記録日数", f"{len(rows)} 日"),
                _row("総摂取", f"{total_intake:.0f} kcal"),
                _row("総消費", f"{total_burn:.0f} kcal"),
                _row("総赤字", f"{total_deficit:+.0f} kcal",
                     color="#E74C3C" if total_deficit < 0 else "#27AE60"),
                _row("1日平均摂取", f"{avg_intake:.0f} kcal"),
                _row("目標達成率", f"{achieve_rate:.0f} %",
                     color="#27AE60" if achieve_rate >= 80 else "#E67E22"),
            ],
        },
    }
    return body


def _bar_row(label: str, value: float, width_str: str, color: str):
    """「ラベル＋数値＋棒 colored box」1行."""
    return {
        "type": "box", "layout": "horizontal", "spacing": "sm",
        "margin": "sm",
        "contents": [
            {"type": "text", "text": f"{label} {value:.0f}",
             "size": "xxs", "color": "#555555", "flex": 2},
            {"type": "box", "layout": "vertical", "flex": 5,
             "contents": [
                {"type": "text", "text": " ", "size": "xxs",
                 "color": "#FFFFFF", "backgroundColor": color},
             ],
             "width": width_str, "height": "10px"},
        ],
    }


def _row(label: str, value: str, color: str = "#333333"):
    return {
        "type": "box",
        "layout": "horizontal",
        "contents": [
            {"type": "text", "text": label, "color": "#555555", "size": "sm"},
            {"type": "text", "text": value, "align": "end",
             "color": color, "size": "sm", "weight": "bold"},
        ],
    }
