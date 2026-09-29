"""Flex Messageの組み立て."""
from typing import List, Dict, Any


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
                "text": "『集計』で再表示 / 『履歴』で7日",
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
