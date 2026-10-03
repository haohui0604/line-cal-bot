"""期間ウィンドウ・体重統計・グラフ用サマリの共通ロジック (Phase 9).

会員画面(/me)とトレーナー画面(/trainer/members/{id})で
「同じ計算・同じ見た目」を使うための共有モジュール。

- window(): 14日などの期間と、過去方向への送り(offset)を解決する
- weight_stats(): 期間内の初回実測→最新実測から「現在値」と「増減」を出す
- build_summary(): カロリー推移・体重推移・目標線に必要な値を一括で返す
"""
from datetime import timedelta
from typing import Any, Dict, Optional

from app.services.dates import today_jst_date
from app.services.db import fetch_day_summary, fetch_weight_series
from app.services.day_view import pfc_percent_series

PAGE_DAYS = 14      # 1ページ＝14日
MAX_OFFSET = 3      # 何ページ前まで遡れるか


def window(days: int = PAGE_DAYS, offset: int = 0):
    """(start, end) を返す。offset=0 が直近、増えるほど過去へ遡る."""
    days = max(1, min(int(days), 90))
    offset = max(0, min(int(offset), 52))
    end = today_jst_date() - timedelta(days=offset * days)
    start = end - timedelta(days=days - 1)
    return start, end


def weight_stats(series) -> Dict[str, Any]:
    """期間内の体重から「現在値」と「期間初日からの増減」を出す.

    記録が無い日は fetch_weight_series が直前の値で繰越済みなので、
    期間初日ちょうどの記録が無くても「その日以前の直近値」が入る。
    増減は 最新 − 初回（マイナス＝減量）。
    """
    vals = [(str(w.get("date") or "")[:10], w.get("weight_kg"))
            for w in (series or [])]
    vals = [(d, v) for d, v in vals if v is not None]
    if not vals:
        return {"current": None, "base": None, "delta": None,
                "current_date": None, "base_date": None}
    base_date, base = vals[0]
    cur_date, cur = vals[-1]
    return {"current": round(float(cur), 1), "base": round(float(base), 1),
            "delta": round(float(cur) - float(base), 1),
            "current_date": cur_date, "base_date": base_date}


def period_nav(days: int = PAGE_DAYS, offset: int = 0,
               base_path: str = "", extra: str = "") -> Dict[str, Any]:
    """期間ナビ（＜過去 / 未来＞）の表示情報を組み立てる.

    base_path="" ならクエリだけ（同一パス内の遷移）、
    それ以外なら base_path 付きのリンクになる。
    extra は cm_offset など引き継ぎたいクエリ。
    """
    off = max(0, min(int(offset), MAX_OFFSET))
    start, end = window(days, off)
    tail = ("&" + extra) if extra else ""

    def href(o: int) -> str:
        return f"{base_path}?offset={o}{tail}"

    return {
        "offset": off,
        "max_offset": MAX_OFFSET,
        "past_href": href(off + 1),                  # ＜ 過去
        "future_href": href(max(0, off - 1)),        # 未来 ＞
        "show_past": off < MAX_OFFSET,
        "show_future": off > 0,
        "range": f"{start.isoformat()} 〜 {end.isoformat()}",
    }


def _targets(user_id: str, on_date: str):
    """期間末時点の「目標摂取kcal」と「目標体重」（未設定なら None）."""
    target_kcal: Optional[float] = None
    target_weight: Optional[float] = None
    try:
        from app.services.db import get_goal
        target_kcal = get_goal(user_id, on_date)
    except Exception:
        target_kcal = None
    try:
        from app.services import goals
        p = goals.get_profile(user_id) or {}
        # goals テーブルに当日以前の目標が無い場合は、目的設定の計算値を目標線に使う
        if target_kcal is None and p.get("calc_target_kcal"):
            target_kcal = float(p["calc_target_kcal"])
        if p.get("goal_mode") == "weight" and p.get("target_weight_kg"):
            target_weight = float(p["target_weight_kg"])
    except Exception:
        pass
    return target_kcal, target_weight


def build_summary(user_id: str, days: int = PAGE_DAYS,
                  offset: int = 0) -> Dict[str, Any]:
    """グラフ用サマリ（会員画面とトレーナー画面で共用）.

    目標線をカロリーグラフの摂取側に引けるよう target_kcal を、
    体重グラフに目標体重線を引けるよう target_weight を返す。
    """
    days = max(1, min(int(days), 90))
    offset = max(0, min(int(offset), 52))
    start, end = window(days, offset)
    labels, intake, burn, pfc = [], [], [], []
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        s = fetch_day_summary(user_id, d)
        labels.append(d[5:])
        intake.append(s.get("intake_kcal") or 0)
        burn.append(s.get("burn_kcal") or s.get("burn") or 0)
        pfc.append((s.get("protein_g"), s.get("fat_g"), s.get("carb_g")))

    w_all = fetch_weight_series(user_id, days=days * (offset + 1)) or []
    _s, _e = start.isoformat(), end.isoformat()
    wseries = [w for w in w_all
               if _s <= str(w.get("date") or "")[:10] <= _e]
    wst = weight_stats(wseries)
    target_kcal, target_weight = _targets(user_id, _e)

    return {
        "labels": labels, "intake": intake, "burn": burn,
        "target_kcal": target_kcal,
        "window_start": _s, "window_end": _e,
        "offset": offset, "days": days,
        "weight_labels": [(w.get("date") or "")[5:] for w in wseries],
        "weight": [w.get("weight_kg") for w in wseries],
        "weight_current": wst["current"],
        "weight_base": wst["base"],
        "weight_delta": wst["delta"],
        "weight_current_date": wst["current_date"],
        "weight_base_date": wst["base_date"],
        "target_weight": target_weight,
        **pfc_percent_series(pfc),
    }
