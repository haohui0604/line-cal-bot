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
from app.services.db import (fetch_bodycomp_series, fetch_day_summary,
                             fetch_weight_series)
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
    try:
        from app.services.goals import goal_schedule
        _gsch = goal_schedule(user_id)
    except Exception:
        _gsch = {}
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

    # ---- 体脂肪率・筋肉量（実測 / 繰越 / BMI推定）----
    try:
        from app.services.goals import body_estimate, bmi_category, get_profile
        _prof = get_profile(user_id) or {}
    except Exception:
        body_estimate, bmi_category, _prof = None, None, {}
    _sex = _prof.get("sex")
    _age = _prof.get("age")
    _hgt = _prof.get("height_cm")
    has_body = bool(_sex and _age and _hgt)

    try:
        _all = fetch_bodycomp_series(user_id, "0001-01-01", _e)
    except Exception:
        _all = []
    _all = sorted(_all, key=lambda r: str(r.get("date") or ""))
    _by_date = {str(r.get("date"))[:10]: r for r in _all}

    # 体重系列（繰越済み）から日付と体重を取り出す
    _days, _w_by_date = [], {}
    for r in (wseries or []):
        if isinstance(r, dict) and r.get("date"):
            _d = str(r["date"])[:10]
            _days.append(_d)
            _w_by_date[_d] = r.get("weight_kg")
    if not _days:
        from datetime import date as _dt, timedelta as _td
        try:
            _sd = _dt.fromisoformat(str(_s)[:10])
            _ed = _dt.fromisoformat(str(_e)[:10])
            _days = [(_sd + _td(days=_k)).isoformat()
                     for _k in range((_ed - _sd).days + 1)]
        except Exception:
            _days = sorted(_by_date.keys())

    # 期間より前の実測を持ち込み（繰越の起点）
    _lf = _lm = None
    for r in _all:
        if str(r.get("date"))[:10] < str(_s)[:10]:
            if r.get("body_fat_pct") is not None:
                _lf = float(r["body_fat_pct"])
            if r.get("muscle_kg") is not None:
                _lm = float(r["muscle_kg"])

    (bc_labels, bc_dates, bc_fat, bc_fat_carry, bc_fat_est,
     bc_mus_m, bc_mus_carry, bc_mus_e, bc_bmi) = ([], [], [], [], [],
                                                  [], [], [], [])
    for _d in _days:
        _rec = _by_date.get(_d)
        _fat_m = _mus_m = None
        if _rec is not None:
            if _rec.get("body_fat_pct") is not None:
                _fat_m = float(_rec["body_fat_pct"])
            if _rec.get("muscle_kg") is not None:
                _mus_m = float(_rec["muscle_kg"])
        _w = _w_by_date.get(_d)
        if _w is None and _rec is not None and _rec.get("weight_kg") is not None:
            _w = float(_rec["weight_kg"])

        _bmi_v = None
        if has_body and _w is not None:
            try:
                _bmi_v = round(float(_w) / ((float(_hgt) / 100.0) ** 2), 1)
            except (TypeError, ValueError, ZeroDivisionError):
                _bmi_v = None

        # 体脂肪率: 実測 → 繰越 → （計測が無い期間は）BMIからの推定
        _est_fat = None
        if _fat_m is None and has_body and body_estimate:
            _e0 = body_estimate(_w, _hgt, _age, _sex)
            _est_fat = _e0["body_fat_pct"] if _e0 else None
        if _fat_m is not None:
            _lf = _fat_m
            _c_fat = None
        elif _lf is not None:
            _c_fat = _lf
        else:
            _c_fat = None
        _eff_fat = _fat_m if _fat_m is not None else _c_fat
        if _eff_fat is None:
            _eff_fat = _est_fat
            _c_fat = None
            _use_est = _est_fat
        else:
            _use_est = None

        # 筋肉量（実測）と繰越
        if _mus_m is not None:
            _lm = _mus_m
            _c_mus = None
        else:
            _c_mus = _lm

        # 除脂肪量 = 体重 × (1 − 体脂肪率)。骨格筋量ではない。
        _lean = None
        if _eff_fat is not None and _w is not None:
            try:
                _lean = round(float(_w) * (1.0 - float(_eff_fat) / 100.0), 1)
            except (TypeError, ValueError):
                _lean = None

        bc_labels.append(_d[5:])
        bc_dates.append(_d)
        bc_fat.append(_fat_m)
        bc_fat_carry.append(_c_fat)
        bc_fat_est.append(_use_est)
        bc_mus_m.append(_mus_m)
        bc_mus_carry.append(_c_mus)
        bc_mus_e.append(_lean)
        bc_bmi.append(_bmi_v)

    def _last(seq):
        for v in reversed(seq or []):
            if v is not None:
                return v
        return None

    def _first(seq):
        for v in (seq or []):
            if v is not None:
                return v
        return None

    _fat_m_c, _fat_m_b = _last(bc_fat), _first(bc_fat)
    _mm_c, _mm_b = _last(bc_mus_m), _first(bc_mus_m)
    _eff_c = _last(bc_fat_carry) if _last(bc_fat_carry) is not None else None
    _fat_eff_seq = [v for v in
                    (bc_fat[i] if bc_fat[i] is not None
                     else (bc_fat_carry[i] if bc_fat_carry[i] is not None
                           else bc_fat_est[i])
                     for i in range(len(bc_fat)))]
    _fat_eff_c = _last(_fat_eff_seq)
    if _fat_m_c is not None:
        _fat_src = "measured"
    elif _last(bc_fat_carry) is not None:
        _fat_src = "carry"
    elif _last(bc_fat_est) is not None:
        _fat_src = "estimate"
    else:
        _fat_src = None
    _fat_date = None
    if _fat_m_c is not None:
        for _i in range(len(bc_fat) - 1, -1, -1):
            if bc_fat[_i] == _fat_m_c:
                _fat_date = bc_dates[_i]
                break
    _lean_c = _last(bc_mus_e)
    _w_eff_c = None
    for _i in range(len(_days) - 1, -1, -1):
        if _w_by_date.get(_days[_i]) is not None:
            _w_eff_c = float(_w_by_date[_days[_i]])
            break
    _fat_mass_c = None
    if _fat_eff_c is not None and _w_eff_c is not None:
        _fat_mass_c = round(_w_eff_c * float(_fat_eff_c) / 100.0, 1)
    _bmi_c = _last(bc_bmi)
    _bmi_target = None
    if target_weight is not None and has_body:
        try:
            _bmi_target = round(float(target_weight) / ((float(_hgt) / 100.0) ** 2), 1)
        except (TypeError, ValueError, ZeroDivisionError):
            _bmi_target = None


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
        # 目標の設定日・期限・残り・ペース
        "goal_set_at": _gsch.get("set_at"),
        "goal_set_at_estimated": _gsch.get("set_at_estimated"),
        "goal_mode": _gsch.get("mode"),
        "goal_target_date": _gsch.get("target_date"),
        "goal_days_left": _gsch.get("days_left"),
        "goal_kg_left": _gsch.get("kg_left"),
        "goal_pace_week": _gsch.get("pace_kg_week"),
        "goal_overdue": _gsch.get("overdue"),
        "goal_achieved": _gsch.get("achieved"),
        # 体脂肪率・筋肉量（実測=実線 / 繰越=破線 / 推定=点線 の材料）
        "bc_labels": bc_labels,
        "bc_dates": bc_dates,
        "bc_body_fat": bc_fat,
        "bc_body_fat_carry": bc_fat_carry,
        "bc_body_fat_est": bc_fat_est,
        "bc_muscle_measured": bc_mus_m,
        "bc_muscle_carry": bc_mus_carry,
        "bc_muscle_estimated": bc_mus_e,
        "bc_bmi": bc_bmi,
        "bc_has_fat": any(v is not None for v in bc_fat),
        "bc_has_fat_carry": any(v is not None for v in bc_fat_carry),
        "bc_has_fat_est": any(v is not None for v in bc_fat_est),
        "bc_has_muscle_measured": any(v is not None for v in bc_mus_m),
        "bc_has_muscle_carry": any(v is not None for v in bc_mus_carry),
        "bc_has_muscle_estimated": any(v is not None for v in bc_mus_e),
        "bc_has_body": has_body,
        "bc_height_cm": _hgt,
        "bc_sex": _sex,
        "bc_age": _age,
        "bc_fat_current": _fat_eff_c,
        "bc_fat_source": _fat_src,
        "bc_fat_delta": (round(_fat_m_c - _fat_m_b, 1)
                         if (_fat_m_c is not None and _fat_m_b is not None) else None),
        "bc_fat_last_date": _fat_date,
        "bc_fat_mass_current": _fat_mass_c,
        "bc_muscle_measured_current": _mm_c,
        "bc_muscle_measured_delta": (round(_mm_c - _mm_b, 1)
                                     if (_mm_c is not None and _mm_b is not None) else None),
        "bc_muscle_carry_current": _last(bc_mus_carry),
        "bc_muscle_estimated_current": _lean_c,
        "bc_lean_current": _lean_c,
        "bc_bmi_current": _bmi_c,
        "bc_bmi_category": (bmi_category(_bmi_c) if (bmi_category and _bmi_c) else None),
        "bc_bmi_target": _bmi_target,
        "bc_weight_current": _w_eff_c,
        **pfc_percent_series(pfc),        **pfc_percent_series(pfc),
    }
