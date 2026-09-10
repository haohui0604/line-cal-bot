"""Flex Message の組み立て."""
from typing import List, Dict, Any, Optional


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

    棒は「背景の薄い箱 (全体幅) の中に、色付きの箱 (kcal比例幅)」の
    二重構造で描く。flex と width を混在させないのがポイント。
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

    bubbles = []
    for r in rows:
        # 全体レーン幅を 200px として比率で棒幅を算出
        intake_w = max(int(r["intake_kcal"] / max_kcal * 200), 3)
        burn_w   = max(int(r["consumed_kcal"] / max_kcal * 200), 3)
        bars = [
            _bar_row("摂取", r["intake_kcal"], intake_w, "#FFB86C"),
            _bar_row("消費", r["consumed_kcal"], burn_w, "#6FA8DC"),
        ]
        if target_kcal:
            target_w = max(int(target_kcal / max_kcal * 200), 3)
            bars.append(_bar_row("目標", target_kcal, target_w, "#9CCC65"))

        contents = [
            {"type": "text", "text": r["date"],
             "weight": "bold", "size": "sm"},
            *bars,
            {"type": "text",
             "text": f"赤字 {r['deficit_kcal']:+.0f} kcal",
             "size": "sm", "weight": "bold", "margin": "md",
             "color": "#E74C3C" if r["deficit_kcal"] < 0 else "#27AE60"},
        ]
        bubbles.append({
            "type": "bubble", "size": "micro",
            "body": {"type": "box", "layout": "vertical",
                     "spacing": "sm", "contents": contents},
        })
    return {"type": "carousel", "contents": bubbles}


def monthly_summary_flex(rows: List[Dict[str, Any]],
                         target_kcal: Optional[float] = None):
    """月次: サマリーバブルのみ (日別カルーセルは週次で確認)."""
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
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "contents": [{"type": "text", "text": "📅 直近30日レポート",
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
        "footer": {
            "type": "box", "layout": "vertical",
            "contents": [{"type": "text",
                          "text": "日別の棒グラフは『週次』で確認できます",
                          "size": "xs", "color": "#888888"}],
        },
    }


def _bar_row(label: str, value: float, bar_width: int, color: str):
    """棒1行: ラベル+数値、背景レーン、その中の色付き棒.

    backgroundColor は box 要素に付ける (text には付けない)。
    flex を使わず固定幅で二重 box にすることで確実に描画される。
    """
    return {
        "type": "box", "layout": "vertical", "spacing": "xs",
        "margin": "sm",
        "contents": [
            {"type": "text",
             "text": f"{label} {value:.0f} kcal",
             "size": "xxs", "color": "#555555"},
            {  # 背景レーン (全体幅)
                "type": "box", "layout": "vertical",
                "width": "200px", "height": "12px",
                "backgroundColor": "#F0F0F0",
                "cornerRadius": "3px",
                "contents": [
                    {  # 色付き棒 (kcal 比例幅)
                        "type": "box", "layout": "vertical",
                        "width": f"{bar_width}px", "height": "12px",
                        "backgroundColor": color,
                        "cornerRadius": "3px",
                        "contents": [],
                    },
                ],
            },
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
