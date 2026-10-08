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

# ================= Phase 10: 継続・達成・ランキング・離脱予兆 =================

from datetime import date as _date

DEFAULT_WINDOW = 30

SLOT_JP_LOCAL = {"breakfast": "朝", "lunch": "昼", "dinner": "夕",
                 "snack": "間食", "night": "夜食"}
MAIN_SLOTS = ("breakfast", "lunch", "dinner")


def _active_members(gym_id: int) -> List[Dict[str, Any]]:
    """ジムの active 会員（担当トレーナー名つき）."""
    try:
        with get_conn() as c:
            rows = c.execute("""
                SELECT m.user_id, m.trainer_id,
                       u.display_name AS member_name,
                       t.display_name AS trainer_name
                  FROM memberships m
                  LEFT JOIN users u ON u.line_user_id = m.user_id
                  LEFT JOIN users t ON t.line_user_id = m.trainer_id
                 WHERE m.gym_id=? AND m.role='member' AND m.status='active'
                 ORDER BY u.display_name, m.id
            """, (gym_id,)).fetchall()
    except Exception:
        logger.exception("_active_members failed")
        return []
    return [dict(r) for r in rows]


def _record_dates(user_ids: List[str], start: str, end: str) -> Dict[str, set]:
    """会員ごとの「記録があった日」の集合（食事・体重・活動の合算）."""
    out: Dict[str, set] = {}
    ids = [u for u in (user_ids or []) if u]
    if not ids:
        return out
    try:
        with get_conn() as c:
            for i in range(0, len(ids), 300):
                chunk = ids[i:i + 300]
                q = ",".join("?" for _ in chunk)
                for table in ("entries", "weight_logs", "activity"):
                    try:
                        rows = c.execute(
                            f"SELECT user_id, date FROM {table}"
                            f" WHERE user_id IN ({q}) AND date>=? AND date<=?",
                            (*chunk, start, end)).fetchall()
                    except Exception:
                        continue
                    for r in rows:
                        d = str(r["date"] or "")[:10]
                        if d:
                            out.setdefault(r["user_id"], set()).add(d)
    except Exception:
        logger.exception("_record_dates failed")
    return out


def _daily_totals(user_id: str, start: str, end: str) -> Dict[str, Dict[str, float]]:
    """日ごとの salt / protein / kcal 合計."""
    try:
        with get_conn() as c:
            rows = c.execute(
                "SELECT date, COALESCE(SUM(salt_g),0) AS salt_g,"
                " COALESCE(SUM(protein_g),0) AS protein_g,"
                " COALESCE(SUM(kcal),0) AS kcal"
                " FROM entries WHERE user_id=? AND date>=? AND date<=?"
                " GROUP BY date", (user_id, start, end)).fetchall()
    except Exception:
        return {}
    return {str(r["date"])[:10]: {"salt_g": r["salt_g"],
                                  "protein_g": r["protein_g"],
                                  "kcal": r["kcal"]} for r in rows}


def _idle_days(today, last: Optional[str]):
    if not last:
        return None
    try:
        return (today - _date.fromisoformat(str(last)[:10])).days
    except Exception:
        return None


# ---- 継続率 ----

def continuity_report(gym_id: int, days: int = DEFAULT_WINDOW) -> Dict[str, Any]:
    """継続率（ライト / 定着 / 週次）と会員ごとの記録状況.

    ライト : 直近7日に1日以上記録
    定着   : 直近7日に3日以上記録
    週次   : 直近4週の各週で1日以上記録
    """
    days = max(7, min(int(days), 365))
    today = today_jst_date()
    end = today.isoformat()
    win_start = (today - timedelta(days=days - 1)).isoformat()
    w7_start = (today - timedelta(days=6)).isoformat()
    w28_start = (today - timedelta(days=27)).isoformat()
    start = min(win_start, w28_start)

    members = _active_members(gym_id)
    ids = [m["user_id"] for m in members]
    dm = _record_dates(ids, start, end)

    rows: List[Dict[str, Any]] = []
    for m in members:
        ds = dm.get(m["user_id"], set())
        w7 = [d for d in ds if d >= w7_start]
        w28 = [d for d in ds if d >= w28_start]
        last = max(ds) if ds else None
        weeks = []
        for k in range(4):
            ws = (today - timedelta(days=6 + 7 * k)).isoformat()
            we = (today - timedelta(days=7 * k)).isoformat()
            weeks.append(any(ws <= d <= we for d in w28))
        rows.append({
            "user_id": m["user_id"],
            "member_name": m.get("member_name") or m["user_id"],
            "trainer_id": m.get("trainer_id") or "",
            "trainer_name": m.get("trainer_name") or "（未割当）",
            "last_date": last,
            "idle_days": _idle_days(today, last),
            "days7": len(w7), "days28": len(w28),
            "light": len(w7) >= 1,
            "settled": len(w7) >= 3,
            "weekly": all(weeks),
        })

    n = len(rows)
    light = len([r for r in rows if r["light"]])
    settled = len([r for r in rows if r["settled"]])
    weekly = len([r for r in rows if r["weekly"]])

    entry_count = 0
    if ids:
        try:
            with get_conn() as c:
                q = ",".join("?" for _ in ids)
                r = c.execute(
                    f"SELECT COUNT(*) AS n FROM entries WHERE user_id IN ({q})"
                    f" AND date>=? AND date<=?",
                    (*ids, win_start, end)).fetchone()
                entry_count = int((r["n"] if r else 0) or 0)
        except Exception:
            entry_count = 0

    return {
        "days": days, "members": n,
        "light": light, "settled": settled, "weekly": weekly,
        "light_pct": round(light * 100.0 / n, 1) if n else None,
        "settled_pct": round(settled * 100.0 / n, 1) if n else None,
        "weekly_pct": round(weekly * 100.0 / n, 1) if n else None,
        "entry_count": entry_count,
        "rows": rows,
    }


# ---- 離脱予兆 ----

def churn_risk(gym_id: int, days: int = DEFAULT_WINDOW,
               yellow: int = 3, red: int = 7) -> List[Dict[str, Any]]:
    """離脱予兆（最終記録日からの経過日数で黄・赤）."""
    cont = continuity_report(gym_id, days=days)
    out: List[Dict[str, Any]] = []
    for r in cont["rows"]:
        idle = r["idle_days"]
        if idle is None or idle >= red:
            level = "red"
        elif idle >= yellow:
            level = "yellow"
        else:
            continue
        out.append({**r, "level": level,
                    "level_label": "赤" if level == "red" else "黄"})
    out.sort(key=lambda r: (-(r["idle_days"] if r["idle_days"] is not None else 9999),
                            r["member_name"]))
    return out


# ---- 目標達成率 ----

def goal_achievement(gym_id: int, days: int = 90) -> Dict[str, Any]:
    """目的別の達成状況.

    減量: 期間内の最新実測が目標体重以下なら達成。
    減塩: 1日の合計 salt_g が目標以下だった日の割合（70%以上を達成扱い）。
    筋肉: 1日の合計 protein_g が目標以上だった日の割合（同上）。
    """
    days = max(1, min(int(days), 365))
    today = today_jst_date()
    end = today.isoformat()
    start = (today - timedelta(days=days - 1)).isoformat()
    out: Dict[str, Any] = {"days": days, "rows": [], "summary": {}}
    try:
        with get_conn() as c:
            if not _has_goal_profiles(c):
                return out
            rows = c.execute("""
                SELECT m.user_id, m.trainer_id,
                       u.display_name AS member_name,
                       t.display_name AS trainer_name, gp.goal_mode,
                       gp.target_weight_kg, gp.salt_target_g, gp.protein_target_g
                  FROM memberships m
                  JOIN goal_profiles gp ON gp.user_id = m.user_id
                  LEFT JOIN users u ON u.line_user_id = m.user_id
                  LEFT JOIN users t ON t.line_user_id = m.trainer_id
                 WHERE m.gym_id=? AND m.role='member' AND m.status='active'
                   AND gp.goal_mode IN ('weight','salt','muscle')
            """, (gym_id,)).fetchall()
    except Exception:
        logger.exception("goal_achievement failed")
        return out

    for r in [dict(x) for x in rows]:
        mode = r["goal_mode"]
        e = {"mode": mode, "label": MODE_JP.get(mode, mode),
             "user_id": r["user_id"], "trainer_id": r.get("trainer_id") or "",
             "member_name": r.get("member_name") or r["user_id"],
             "trainer_name": r.get("trainer_name") or "（未割当）",
             "target": None, "value": None, "rate": None, "achieved": False}
        if mode == "weight":
            tw = r.get("target_weight_kg")
            e["target"] = tw
            prog = member_progress(r["user_id"], days=days, target_weight=tw)
            e["value"] = prog["latest"]
            if tw and prog["latest"] is not None:
                e["achieved"] = float(prog["latest"]) <= float(tw)
                e["rate"] = prog.get("pct_to_goal")
        else:
            tgt = r.get("salt_target_g") if mode == "salt" else r.get("protein_target_g")
            e["target"] = tgt
            if tgt:
                dailies = _daily_totals(r["user_id"], start, end)
                if dailies:
                    if mode == "salt":
                        ok = sum(1 for v in dailies.values()
                                 if float(v["salt_g"] or 0) <= float(tgt))
                    else:
                        ok = sum(1 for v in dailies.values()
                                 if float(v["protein_g"] or 0) >= float(tgt))
                    rate = round(ok * 100.0 / len(dailies), 1)
                    e["rate"] = rate
                    e["value"] = f"{ok}/{len(dailies)}日"
                    e["achieved"] = rate >= 70.0
        out["rows"].append(e)

    for mode, label in MODE_JP.items():
        rs = [x for x in out["rows"] if x["mode"] == mode]
        n = len(rs)
        ach = len([x for x in rs if x["achieved"]])
        rates = [x["rate"] for x in rs if x["rate"] is not None]
        out["summary"][mode] = {
            "label": label, "members": n, "achieved": ach,
            "pct": round(ach * 100.0 / n, 1) if n else None,
            "avg_rate": round(sum(rates) / len(rates), 1) if rates else None,
        }
    return out


# ---- トレーナーランキング ----

def trainer_ranking(gym_id: int, days: int = 90) -> List[Dict[str, Any]]:
    """トレーナー別の担当数・継続率・コメント数・未返信・平均減量."""
    days = max(1, min(int(days), 365))
    cont = continuity_report(gym_id, days=min(days, 90))
    since = (today_jst_date() - timedelta(days=29)).isoformat()

    comments: Dict[str, int] = {}
    try:
        with get_conn() as c:
            for r in c.execute(
                "SELECT author_id AS a, COUNT(*) AS n FROM comments"
                " WHERE author_type='trainer' AND date(created_at) >= ?"
                " GROUP BY author_id", (since,)).fetchall():
                comments[str(r["a"] or "")] = int(r["n"])
    except Exception:
        comments = {}

    loss: Dict[str, Dict[str, Any]] = {}
    try:
        rep = trainer_loss_report(gym_id, days=days)
        loss = {str(r["trainer_id"] or ""): r for r in rep["rows"]}
    except Exception:
        loss = {}

    try:
        from app.services import gym_db
    except Exception:
        gym_db = None

    by: Dict[str, Dict[str, Any]] = {}
    for r in cont["rows"]:
        tid = str(r.get("trainer_id") or "")
        b = by.setdefault(tid, {
            "trainer_id": tid,
            "trainer_name": r.get("trainer_name") or "（未割当）",
            "members": 0, "light": 0, "settled": 0, "idle": 0})
        b["members"] += 1
        if r["light"]:
            b["light"] += 1
        if r["settled"]:
            b["settled"] += 1
        if r["idle_days"] is None or r["idle_days"] >= 7:
            b["idle"] += 1

    out: List[Dict[str, Any]] = []
    for b in by.values():
        n = b["members"]
        lr = loss.get(b["trainer_id"]) or {}
        unread = 0
        if b["trainer_id"] and gym_db is not None:
            try:
                unread = gym_db.count_unread_member_comments(b["trainer_id"])
            except Exception:
                unread = 0
        out.append({
            "trainer_id": b["trainer_id"],
            "trainer_name": b["trainer_name"],
            "members": n,
            "light_pct": round(b["light"] * 100.0 / n, 1) if n else None,
            "settled_pct": round(b["settled"] * 100.0 / n, 1) if n else None,
            "idle": b["idle"],
            "comments_30d": comments.get(b["trainer_id"], 0),
            "unread": unread,
            "avg_loss": lr.get("avg_loss"),
            "measured": lr.get("measured"),
        })
    out.sort(key=lambda r: (-(r["light_pct"] or 0), -(r["comments_30d"] or 0),
                            r["trainer_name"]))
    return out


def comment_correlation(gym_id: int, days: int = 30) -> List[Dict[str, Any]]:
    """「コメント数」と「継続率・目標達成率」の関係（トレーナー単位）."""
    days = max(7, min(int(days), 365))
    rank = trainer_ranking(gym_id, days=days)
    achv = goal_achievement(gym_id, days=max(days, 90))
    by_tr: Dict[str, Dict[str, int]] = {}
    for e in achv.get("rows", []):
        tid = str(e.get("trainer_id") or "")
        b = by_tr.setdefault(tid, {"n": 0, "ach": 0})
        b["n"] += 1
        if e.get("achieved"):
            b["ach"] += 1
    out: List[Dict[str, Any]] = []
    for r in rank:
        b = by_tr.get(r["trainer_id"]) or {"n": 0, "ach": 0}
        out.append({
            "trainer_name": r["trainer_name"],
            "comments_30d": r["comments_30d"],
            "light_pct": r["light_pct"],
            "settled_pct": r["settled_pct"],
            "goal_members": b["n"],
            "achieved_pct": round(b["ach"] * 100.0 / b["n"], 1) if b["n"] else None,
        })
    out.sort(key=lambda r: -(r["comments_30d"] or 0))
    return out


# ---- 週次トレンド ----

def weekly_trend(gym_id: int, weeks: int = 8) -> List[Dict[str, Any]]:
    """週ごとの 記録した会員数（延べ）・平均摂取kcal・平均消費kcal."""
    weeks = max(1, min(int(weeks), 26))
    today = today_jst_date()
    start = (today - timedelta(days=weeks * 7 - 1)).isoformat()
    end = today.isoformat()
    ent: Dict[str, Dict[str, float]] = {}
    act: Dict[str, Dict[str, float]] = {}
    try:
        with get_conn() as c:
            for r in c.execute("""
                SELECT e.date AS d, e.user_id AS u,
                       COALESCE(SUM(e.kcal),0) AS kcal
                  FROM entries e
                  JOIN memberships m ON m.user_id = e.user_id
                   AND m.gym_id=? AND m.role='member' AND m.status='active'
                 WHERE e.date>=? AND e.date<=?
                 GROUP BY e.date, e.user_id
            """, (gym_id, start, end)).fetchall():
                ent.setdefault(str(r["d"])[:10], {})[r["u"]] = float(r["kcal"] or 0)
            for r in c.execute("""
                SELECT a.date AS d, a.user_id AS u,
                       COALESCE(SUM(a.total_kcal),0) AS kcal
                  FROM activity a
                  JOIN memberships m ON m.user_id = a.user_id
                   AND m.gym_id=? AND m.role='member' AND m.status='active'
                 WHERE a.date>=? AND a.date<=?
                 GROUP BY a.date, a.user_id
            """, (gym_id, start, end)).fetchall():
                act.setdefault(str(r["d"])[:10], {})[r["u"]] = float(r["kcal"] or 0)
    except Exception:
        logger.exception("weekly_trend failed")
        return []

    out: List[Dict[str, Any]] = []
    for k in range(weeks - 1, -1, -1):
        ws = (today - timedelta(days=6 + 7 * k)).isoformat()
        we = (today - timedelta(days=7 * k)).isoformat()
        ik = 0.0
        pairs = 0
        users = set()
        for d, mp in ent.items():
            if ws <= d <= we:
                ik += sum(mp.values())
                pairs += len(mp)
                users |= set(mp.keys())
        bn = 0.0
        bcount = 0
        for d, mp in act.items():
            if ws <= d <= we:
                bn += sum(mp.values())
                bcount += len(mp)
        out.append({
            "label": f"{ws[5:]}〜{we[5:]}",
            "members": len(users),
            "member_days": pairs,
            "avg_intake": round(ik / pairs) if pairs else None,
            "avg_burn": round(bn / bcount) if bcount else None,
        })
    return out


# ---- 記録の質 ----

SOURCE_JP = {"llm_estimate": "テキスト推定", "ocr_label": "成分表",
             "ocr_photo": "写真", "photo": "写真", "manual": "手入力"}


def data_quality(gym_id: int, days: int = 90) -> Dict[str, Any]:
    """記録の内訳（テキスト/成分表/写真）と実測体重の割合."""
    days = max(1, min(int(days), 365))
    today = today_jst_date()
    end = today.isoformat()
    start = (today - timedelta(days=days - 1)).isoformat()
    src: Dict[str, int] = {}
    measured = 0
    total_w = 0
    try:
        with get_conn() as c:
            for r in c.execute("""
                SELECT e.source_type AS s, COUNT(*) AS n FROM entries e
                JOIN memberships m ON m.user_id = e.user_id
                 AND m.gym_id=? AND m.role='member' AND m.status='active'
                WHERE e.date>=? AND e.date<=? GROUP BY e.source_type
            """, (gym_id, start, end)).fetchall():
                src[str(r["s"] or "unknown")] = int(r["n"])
            r = c.execute("""
                SELECT COALESCE(SUM(CASE WHEN w.is_measured=1 THEN 1 ELSE 0 END),0) AS m,
                       COUNT(*) AS n FROM weight_logs w
                JOIN memberships mm ON mm.user_id = w.user_id
                 AND mm.gym_id=? AND mm.role='member' AND mm.status='active'
                WHERE w.date>=? AND w.date<=?
            """, (gym_id, start, end)).fetchone()
            measured = int((r["m"] if r else 0) or 0)
            total_w = int((r["n"] if r else 0) or 0)
    except Exception:
        logger.exception("data_quality failed")
    total_e = sum(src.values())
    ordered = sorted(src.items(), key=lambda kv: -kv[1])
    return {
        "days": days, "total_entries": total_e,
        "sources": [{"key": k, "label": SOURCE_JP.get(k, k), "count": v,
                     "pct": round(v * 100.0 / total_e, 1) if total_e else 0.0}
                    for k, v in ordered],
        "source_text": " / ".join(
            f"{SOURCE_JP.get(k, k)} {v}件" for k, v in ordered) or "記録なし",
        "measured": measured, "weight_records": total_w,
        "measured_pct": round(measured * 100.0 / total_w, 1) if total_w else None,
    }


# ---- トレーナー画面BI ----

def trainer_overview(staff_id: str, days: int = DEFAULT_WINDOW) -> Dict[str, Any]:
    """トレーナー画面用BI（担当会員だけを集計）."""
    days = max(7, min(int(days), 365))
    today = today_jst_date()
    end = today.isoformat()
    start = (today - timedelta(days=days - 1)).isoformat()
    w7_start = (today - timedelta(days=6)).isoformat()

    try:
        with get_conn() as c:
            rows = c.execute("""
                SELECT m.user_id, u.display_name AS member_name
                  FROM memberships m
                  LEFT JOIN users u ON u.line_user_id = m.user_id
                 WHERE m.role='member' AND m.status='active' AND m.trainer_id=?
                 ORDER BY u.display_name, m.id
            """, (staff_id,)).fetchall()
    except Exception:
        logger.exception("trainer_overview failed")
        return {"days": days, "total": 0, "rows": [], "churn": [],
                "no_record_today": [], "missing_slots": [],
                "diff": {"over": 0, "under": 0, "unknown": 0, "avg": None},
                "progress": [], "unread": 0}

    members = [dict(r) for r in rows]
    ids = [m["user_id"] for m in members]
    dm = _record_dates(ids, start, end)

    today_intake: Dict[str, float] = {}
    today_slots: Dict[str, set] = {}
    if ids:
        try:
            with get_conn() as c:
                q = ",".join("?" for _ in ids)
                for r in c.execute(
                    f"SELECT user_id, meal_slot, COALESCE(SUM(kcal),0) AS k"
                    f" FROM entries WHERE date=? AND user_id IN ({q})"
                    f" GROUP BY user_id, meal_slot", (end, *ids)).fetchall():
                    uid = r["user_id"]
                    today_intake[uid] = today_intake.get(uid, 0.0) + float(r["k"] or 0)
                    today_slots.setdefault(uid, set()).add(str(r["meal_slot"]))
        except Exception:
            pass

    from app.services.db import get_goal
    from app.services import goals as _goals

    def _target(uid):
        try:
            t = get_goal(uid, end)
            if t:
                return float(t)
        except Exception:
            pass
        try:
            p = _goals.get_profile(uid) or {}
            if p.get("calc_target_kcal"):
                return float(p["calc_target_kcal"])
        except Exception:
            pass
        return None

    rows_out: List[Dict[str, Any]] = []
    over = under = unknown = 0
    diffs: List[float] = []
    churn: List[Dict[str, Any]] = []
    no_rec: List[str] = []
    missing: List[Dict[str, Any]] = []

    for m in members:
        uid = m["user_id"]
        ds = dm.get(uid, set())
        last = max(ds) if ds else None
        idle = _idle_days(today, last)
        w7 = len([d for d in ds if d >= w7_start])
        intake = today_intake.get(uid)
        tg = _target(uid)
        diff = None
        if tg is not None and intake is not None:
            diff = round(intake - tg)
            diffs.append(diff)
            if diff > 0:
                over += 1
            else:
                under += 1
        elif tg is None:
            unknown += 1
        badge = None
        try:
            badge = _goals.goal_badge(uid)
        except Exception:
            badge = None
        row = {
            "user_id": uid,
            "member_name": m.get("member_name") or uid,
            "last_date": last, "idle_days": idle, "days7": w7,
            "today_intake": round(intake) if intake is not None else None,
            "target_kcal": round(tg) if tg is not None else None,
            "diff": diff,
            "goal_label": (badge or {}).get("label"),
            "light": w7 >= 1,
        }
        rows_out.append(row)
        if idle is None or idle >= 7:
            churn.append({**row, "level": "red", "level_label": "赤"})
        elif idle >= 3:
            churn.append({**row, "level": "yellow", "level_label": "黄"})
        if uid not in today_slots:
            no_rec.append(row["member_name"])
        else:
            slots = today_slots.get(uid) or set()
            miss = [SLOT_JP_LOCAL[s] for s in MAIN_SLOTS if s not in slots]
            if miss:
                missing.append({"member_name": row["member_name"],
                                "missing": miss})

    churn.sort(key=lambda r: (-(r["idle_days"] if r["idle_days"] is not None else 9999),
                              r["member_name"]))

    progress: List[Dict[str, Any]] = []
    try:
        with get_conn() as c:
            if _has_goal_profiles(c) and ids:
                q = ",".join("?" for _ in ids)
                for r in c.execute(
                    f"SELECT user_id, target_weight_kg FROM goal_profiles"
                    f" WHERE goal_mode='weight' AND user_id IN ({q})", ids).fetchall():
                    uid = r["user_id"]
                    pr = member_progress(uid, days=days,
                                         target_weight=r["target_weight_kg"])
                    if pr["delta"] is None:
                        continue
                    progress.append({
                        "member_name": next((x["member_name"] for x in rows_out
                                             if x["user_id"] == uid), uid),
                        "delta": pr["delta"], "pct_to_goal": pr["pct_to_goal"],
                        "latest": pr["latest"], "points": pr["points"],
                    })
    except Exception:
        progress = []
    progress.sort(key=lambda r: (r["delta"], r["member_name"]))
    progress = progress[:5]

    n = len(rows_out)
    light = len([r for r in rows_out if r["light"]])
    settled = len([r for r in rows_out if r["days7"] >= 3])
    unread = 0
    try:
        from app.services import gym_db
        unread = gym_db.count_unread_member_comments(staff_id)
    except Exception:
        unread = 0

    return {
        "days": days, "total": n,
        "light": light, "light_pct": round(light * 100.0 / n, 1) if n else None,
        "settled": settled, "settled_pct": round(settled * 100.0 / n, 1) if n else None,
        "idle": len([r for r in rows_out
                     if r["idle_days"] is None or r["idle_days"] >= 7]),
        "rows": rows_out,
        "churn": churn[:5],
        "no_record_today": no_rec,
        "missing_slots": missing[:10],
        "diff": {"over": over, "under": under, "unknown": unknown,
                 "avg": round(sum(diffs) / len(diffs)) if diffs else None},
        "progress": progress,
        "unread": unread,
    }
