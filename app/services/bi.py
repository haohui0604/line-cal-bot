"""ジム管理のBI集計 (Phase 9).

1) 目的設定の内訳（減量 / 減塩 / 筋肉）
   → 分母は「目的を設定している会員」だけ（未設定は数えない）
2) 減量目的の会員の体重実測から、トレーナーごとの減量実績
   → 会員1人ごとに「期間内の初回実測 → 最新実測」の増減を出し、
     トレーナー単位で 対象人数 / 減量できた人数 / 平均減量 / 合計減量 を集計。
     実測が2点未満の会員は「計測不足」として別枠で数える（平均から除外）。
"""
import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional

from app.services.dates import today_jst_date
from app.services.db import get_conn, table_columns

logger = logging.getLogger(__name__)

MODE_JP = {"weight": "減量", "salt": "減塩", "muscle": "筋肉"}
DEFAULT_DAYS = 90


def _has_goal_profiles(c) -> bool:
    try:
        return bool(table_columns(c, "goal_profiles"))
    except Exception:
        return False


# ---- 1) 目的設定の内訳 ----

def goal_distribution(gym_id: int) -> Dict[str, Any]:
    """目的を設定している会員の内訳と百分率（未設定の会員は分母に入れない）."""
    out: Dict[str, Any] = {"total": 0, "counts": {}, "pct": {}, "labels": MODE_JP}
    try:
        with get_conn() as c:
            if not _has_goal_profiles(c):
                return out
            rows = c.execute("""
                SELECT gp.goal_mode AS mode, COUNT(*) AS n
                  FROM memberships m
                  JOIN goal_profiles gp ON gp.user_id = m.user_id
                 WHERE m.gym_id=? AND m.role='member' AND m.status='active'
                   AND gp.goal_mode IN ('weight','salt','muscle')
                 GROUP BY gp.goal_mode
            """, (gym_id,)).fetchall()
    except Exception:
        logger.exception("goal_distribution failed")
        return out
    total = sum(int(r["n"]) for r in rows)
    counts = {r["mode"]: int(r["n"]) for r in rows}
    out["total"] = total
    out["counts"] = counts
    out["pct"] = {k: (round(v * 100.0 / total, 1) if total else 0.0)
                  for k, v in counts.items()}
    return out


# ---- 2) 体重の実測から減量実績 ----

def weight_points(user_id: str, days: int = DEFAULT_DAYS,
                  end_date: Optional[str] = None) -> List[Dict[str, Any]]:
    """直近days日の体重記録（実測のみ・日付昇順）."""
    end = end_date or today_jst_date().isoformat()
    start = (today_jst_date() - timedelta(days=max(1, int(days)) - 1)).isoformat()
    with get_conn() as c:
        rows = c.execute(
            "SELECT date, weight_kg FROM weight_logs"
            " WHERE user_id=? AND date>=? AND date<=?"
            " ORDER BY date", (user_id, start, end)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("weight_kg") is not None:
            out.append(d)
    return out


def member_progress(user_id: str, days: int = DEFAULT_DAYS,
                    target_weight: Optional[float] = None) -> Dict[str, Any]:
    """1会員の減量進捗（期間内の初回実測と最新実測から算出）.

    delta = 最新 − 初回（マイナス＝減量）。
    実測が2点未満なら delta=None（「計測不足」として扱う）。
    pct_to_goal = (初回 − 最新) / (初回 − 目標体重) × 100。
    """
    pts = weight_points(user_id, days=days)
    base: Dict[str, Any] = {
        "points": len(pts), "first": None, "first_date": None,
        "latest": None, "latest_date": None, "delta": None,
        "target_weight": target_weight, "pct_to_goal": None,
    }
    if len(pts) < 2:
        return base
    f, l = pts[0], pts[-1]
    first = float(f["weight_kg"])
    latest = float(l["weight_kg"])
    pct = None
    if target_weight:
        span = first - float(target_weight)
        if span > 0:
            pct = round((first - latest) * 100.0 / span, 1)
    return {
        "points": len(pts),
        "first": round(first, 1), "first_date": f["date"],
        "latest": round(latest, 1), "latest_date": l["date"],
        "delta": round(latest - first, 1),
        "target_weight": target_weight, "pct_to_goal": pct,
    }


def weight_goal_members(gym_id: int) -> List[Dict[str, Any]]:
    """減量目的の会員（担当トレーナー・目標体重つき）."""
    try:
        with get_conn() as c:
            if not _has_goal_profiles(c):
                return []
            rows = c.execute("""
                SELECT m.user_id, m.trainer_id,
                       u.display_name, t.display_name AS trainer_name,
                       gp.target_weight_kg, gp.goal_days
                  FROM memberships m
                  JOIN goal_profiles gp ON gp.user_id = m.user_id
                  LEFT JOIN users u ON u.line_user_id = m.user_id
                  LEFT JOIN users t ON t.line_user_id = m.trainer_id
                 WHERE m.gym_id=? AND m.role='member' AND m.status='active'
                   AND gp.goal_mode='weight'
                 ORDER BY t.display_name, u.display_name
            """, (gym_id,)).fetchall()
    except Exception:
        logger.exception("weight_goal_members failed")
        return []
    return [dict(r) for r in rows]


def trainer_loss_report(gym_id: int, days: int = DEFAULT_DAYS) -> Dict[str, Any]:
    """トレーナーごとの減量実績（減量目的の会員のみが対象）."""
    days = max(1, min(int(days), 365))
    members = weight_goal_members(gym_id)
    by_tr: Dict[str, Dict[str, Any]] = {}
    details: List[Dict[str, Any]] = []

    for m in members:
        tr = m.get("trainer_id") or ""
        prog = member_progress(m["user_id"], days=days,
                               target_weight=m.get("target_weight_kg"))
        details.append({
            "user_id": m["user_id"],
            "member_name": m.get("display_name") or m["user_id"],
            "trainer_id": tr,
            "trainer_name": m.get("trainer_name") or "（未割当）",
            "goal_days": m.get("goal_days"),
            **prog,
        })
        b = by_tr.setdefault(tr, {
            "trainer_id": tr,
            "trainer_name": m.get("trainer_name") or "（未割当）",
            "members": 0, "measured": 0, "lost": 0, "insufficient": 0,
            "delta_sum": 0.0, "deltas": [], "pcts": [],
        })
        b["members"] += 1
        if prog["delta"] is None:
            b["insufficient"] += 1
        else:
            b["measured"] += 1
            if prog["delta"] < 0:
                b["lost"] += 1
            b["delta_sum"] += prog["delta"]
            b["deltas"].append(prog["delta"])
            if prog["pct_to_goal"] is not None:
                b["pcts"].append(prog["pct_to_goal"])

    rows: List[Dict[str, Any]] = []
    for b in by_tr.values():
        n = b["measured"]
        rows.append({
            "trainer_id": b["trainer_id"],
            "trainer_name": b["trainer_name"],
            "members": b["members"],
            "measured": n,
            "lost": b["lost"],
            "insufficient": b["insufficient"],
            "avg_delta": round(b["delta_sum"] / n, 2) if n else None,
            "avg_loss": round(-b["delta_sum"] / n, 2) if n else None,
            "total_loss": round(-b["delta_sum"], 2) if n else None,
            "best_loss": round(-min(b["deltas"]), 1) if b["deltas"] else None,
            "worst_loss": round(-max(b["deltas"]), 1) if b["deltas"] else None,
            "avg_pct": round(sum(b["pcts"]) / len(b["pcts"]), 1) if b["pcts"] else None,
        })
    rows.sort(key=lambda r: (r["avg_loss"] is None, -(r["avg_loss"] or 0)))

    measured = [d for d in details if d["delta"] is not None]
    total_loss = round(-sum(d["delta"] for d in measured), 2) if measured else None
    summary = {
        "members": len(details),
        "measured": len(measured),
        "lost": len([d for d in measured if d["delta"] < 0]),
        "insufficient": len(details) - len(measured),
        "avg_loss": (round(-sum(d["delta"] for d in measured) / len(measured), 2)
                     if measured else None),
        "total_loss": total_loss,
    }
    return {"days": days, "rows": rows, "details": details, "summary": summary}
