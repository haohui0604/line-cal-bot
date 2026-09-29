"""matplotlib で PNG チャートを生成.

cal_full.py の遺産を受け継ぎ、SQLite を直接ソースにする。
返り値は io.BytesIO (PNG)."""
import io
import sqlite3
from datetime import date, timedelta
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from app.config import settings


def render_user_history(user_id: str, days: int = 14) -> io.BytesIO:
    end = date.today()
    start = end - timedelta(days=days - 1)
    conn = sqlite3.connect(settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    intake_rows = conn.execute("""
        SELECT date, ROUND(SUM(kcal),0) AS kcal
        FROM entries
        WHERE user_id=? AND date BETWEEN ? AND ?
        GROUP BY date ORDER BY date
    """, (user_id, start.isoformat(), end.isoformat())).fetchall()
    activity_rows = conn.execute("""
        SELECT date, total_kcal AS kcal
        FROM activity
        WHERE user_id=? AND date BETWEEN ? AND ?
        ORDER BY date
    """, (user_id, start.isoformat(), end.isoformat())).fetchall()
    conn.close()

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 5.5),
                                     gridspec_kw={"height_ratios": [2, 1]})
    fig.suptitle(f"Calorie Log  ({start} 〜 {end})", fontsize=12)

    intake_map = {r["date"]: r["kcal"] for r in intake_rows}
    activity_map = {r["date"]: r["kcal"] for r in activity_rows}
    dates, intake_vals, burn_vals, deficit_vals = [], [], [], []
    cur = start
    while cur <= end:
        dates.append(cur)
        i = intake_map.get(cur.isoformat(), 0)
        b = activity_map.get(cur.isoformat(), 0)
        intake_vals.append(i)
        burn_vals.append(b)
        deficit_vals.append(b - i)
        cur += timedelta(days=1)

    ax1.bar(dates, intake_vals, label="摂取", color="#3B82F6", alpha=0.85)
    ax1.bar(dates, burn_vals, label="消費", color="#EF4444", alpha=0.85)
    ax1.set_ylabel("kcal")
    ax1.legend(loc="upper left", fontsize=8)
    ax1.grid(True, axis="y", alpha=0.3)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))

    cum_deficit = []
    s = 0
    for d in deficit_vals:
        s += d
        cum_deficit.append(s)
    ax2.fill_between(dates, cum_deficit, color="#10B981", alpha=0.6)
    ax2.axhline(0, color="black", lw=0.5)
    ax2.set_ylabel("累計赤字 (kcal)")
    ax2.grid(True, axis="y", alpha=0.3)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))

    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf
