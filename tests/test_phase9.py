"""Phase 9: 会員/トレーナー画面の共通化とジムBI.

- 体重推移の「現在値・増減・期間切替・目標体重線」を会員画面とトレーナー画面で共通化
- 日別の収支＝摂取−消費 / 目標との差＝摂取−目標、目標線は摂取バーの上
- 会員一覧に目的（設定している場合のみ）
- ジムBI: 目的の百分率・トレーナーごとの減量実績
"""
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DB_PATH", str(Path(tempfile.mkdtemp()) / "phase9.db"))
os.environ.setdefault("TURSO_DATABASE_URL", "")

from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services import bi, db, goals, gym_db, periods  # noqa: E402
from app.services.dates import today_jst_date  # noqa: E402
import app.web.member as member_web  # noqa: E402

ADMIN = "Up9_admin"
TRAINER = "Up9_tr"
MEMBER = "Up9_mem"
MEMBER2 = "Up9_mem2"
GID = 1
client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _db():
    settings.DB_PATH = str(Path(tempfile.mkdtemp()) / "gym_phase9.db")
    settings.ADMIN_USER_IDS = ADMIN
    db.init_db()
    yield


@pytest.fixture(autouse=True)
def _seed(_db):
    global GID
    gyms = gym_db.list_admin_gyms(ADMIN)
    GID = int(gyms[0]["gym_id"]) if gyms else int(
        gym_db.create_gym(name="Phase9ジム", owner_user_id=ADMIN)["id"])
    with db.get_conn() as c:
        for uid, nm in ((ADMIN, "管理者"), (TRAINER, "担当"),
                        (MEMBER, "会員A"), (MEMBER2, "会員B")):
            c.execute("INSERT OR REPLACE INTO users (line_user_id, display_name)"
                      " VALUES (?,?)", (uid, nm))
        for uid, role in ((ADMIN, "gym_admin"), (TRAINER, "trainer")):
            c.execute("INSERT OR IGNORE INTO memberships (gym_id,user_id,role,status)"
                      " VALUES (?,?,?,'active')", (GID, uid, role))
        for uid in (MEMBER, MEMBER2):
            c.execute("INSERT OR IGNORE INTO memberships"
                      " (gym_id,user_id,role,status,trainer_id)"
                      " VALUES (?,?,'member','active',?)", (GID, uid, TRAINER))
            c.execute("UPDATE memberships SET status='active', trainer_id=?,"
                      " data_share_scope='assigned', consent_at=NULL"
                      " WHERE gym_id=? AND user_id=? AND role='member'",
                      (TRAINER, GID, uid))
        c.execute("DELETE FROM weight_logs WHERE user_id IN (?,?)", (MEMBER, MEMBER2))
    today = today_jst_date()
    # 会員A: 90.0 → 89.0 → 88.0 kg（-2.0kg、目標85kg）
    for i, w in ((60, 90.0), (30, 89.0), (1, 88.0)):
        db.save_weight(user_id=MEMBER, date=(today - timedelta(days=i)).isoformat(),
                       weight_kg=w, is_measured=1)
    # 会員B: 計測1点のみ（計測不足の検証用）。目的は減塩なので減量集計の対象外
    db.save_weight(user_id=MEMBER2, date=(today - timedelta(days=10)).isoformat(),
                   weight_kg=70.0, is_measured=1)
    goals.save_profile(MEMBER, goal_mode="weight", target_weight_kg=85.0,
                       goal_days=90, calc_target_kcal=1800)
    goals.save_profile(MEMBER2, goal_mode="salt", salt_target_g=6.0)
    yield


# ---- 共通ロジック ----

def test_window_and_weight_stats():
    s0, e0 = periods.window(14, 0)
    assert (e0 - s0).days == 13
    assert (periods.window(14, 0)[1] - periods.window(14, 1)[1]).days == 14
    st = periods.weight_stats([{"date": "2026-09-20", "weight_kg": 70.0},
                               {"date": "2026-10-03", "weight_kg": 68.5}])
    assert st["current"] == 68.5 and st["base"] == 70.0
    assert st["delta"] == -1.5
    assert periods.weight_stats([])["delta"] is None


def test_period_nav_hrefs():
    nav = periods.period_nav(14, 0, base_path="/trainer/members/X", extra="cm_offset=30")
    assert nav["show_past"] and not nav["show_future"]
    assert nav["past_href"] == "/trainer/members/X?offset=1&cm_offset=30"
    assert nav["future_href"] == "/trainer/members/X?offset=0&cm_offset=30"
    nav2 = periods.period_nav(14, 2)
    assert nav2["future_href"] == "?offset=1"


def test_goal_badges_only_when_set():
    b = goals.goal_badge(MEMBER)
    assert b["label"] == "減量" and b["detail"].startswith("85.0kg")
    assert goals.goal_badge("nobody_at_all") is None
    m = goals.list_goal_badges([MEMBER, MEMBER2, "nobody_at_all"])
    assert set(m.keys()) == {MEMBER, MEMBER2}
    assert m[MEMBER2]["label"] == "減塩"
    assert goals.list_goal_badges([]) == {}


# ---- BI 集計 ----

def test_bi_goal_distribution():
    dist = bi.goal_distribution(GID)
    assert dist["counts"] == {"weight": 1, "salt": 1}
    assert dist["total"] == 2
    assert dist["pct"]["weight"] == 50.0 and dist["pct"]["salt"] == 50.0


def test_bi_trainer_loss_report():
    rep = bi.trainer_loss_report(GID, days=90)
    assert rep["summary"]["members"] == 1          # 減量目的の会員だけ
    assert rep["summary"]["measured"] == 1
    assert rep["summary"]["avg_loss"] == 2.0
    assert rep["summary"]["total_loss"] == 2.0
    row = [r for r in rep["rows"] if r["trainer_id"] == TRAINER][0]
    assert row["members"] == 1 and row["measured"] == 1 and row["insufficient"] == 0
    assert row["avg_loss"] == 2.0 and row["best_loss"] == 2.0
    d = [x for x in rep["details"] if x["user_id"] == MEMBER][0]
    assert (d["first"], d["latest"], d["delta"]) == (90.0, 88.0, -2.0)
    assert d["pct_to_goal"] == 40.0                # (90-88)/(90-85) = 40%


def test_bi_insufficient_measurement_is_excluded():
    """実測1点しかない減量目的の会員は平均から除外される."""
    with db.get_conn() as c:
        c.execute("INSERT OR IGNORE INTO memberships"
                  " (gym_id,user_id,role,status,trainer_id)"
                  " VALUES (?,?,'member','active',?)", (GID, "Up9_mem3", TRAINER))
    goals.save_profile("Up9_mem3", goal_mode="weight", target_weight_kg=60.0)
    db.save_weight(user_id="Up9_mem3",
                   date=(today_jst_date() - timedelta(days=5)).isoformat(),
                   weight_kg=65.0, is_measured=1)
    rep = bi.trainer_loss_report(GID, days=90)
    assert rep["summary"]["members"] == 2
    assert rep["summary"]["measured"] == 1         # 1点だけの会員は数えない
    assert rep["summary"]["insufficient"] == 1
    assert rep["summary"]["avg_loss"] == 2.0
    row = [r for r in rep["rows"] if r["trainer_id"] == TRAINER][0]
    assert row["members"] == 2 and row["measured"] == 1 and row["insufficient"] == 1


# ---- API / 画面 ----

def test_trainer_summary_api_has_target_and_delta():
    ck = {"session": auth.issue_session(TRAINER)}
    r = client.get(f"/api/trainer/members/{MEMBER}/summary?days=14&offset=0",
                   cookies=ck)
    assert r.status_code == 200
    d = r.json()
    assert len(d["labels"]) == 14
    assert d["weight_current"] == 88.0
    # 14日窓の基準は「窓初日時点の繰越値」(90.0→89.0) なので -1.0 kg
    assert d["weight_delta"] == -1.0
    assert d["weight_base"] == 89.0
    assert d["target_weight"] == 85.0
    assert "target_kcal" in d
    r2 = client.get(f"/api/trainer/members/{MEMBER}/summary?days=14&offset=1",
                    cookies=ck)
    assert r2.status_code == 200
    j2 = r2.json()
    assert j2["offset"] == 1
    assert j2["window_end"] < d["window_end"]
    assert j2["window_end"] == (today_jst_date() - timedelta(days=14)).isoformat()


def test_member_summary_api_uses_shared_logic(monkeypatch):
    monkeypatch.setattr(member_web, "_verify_uid", lambda t: MEMBER)
    r = client.post("/api/me/summary", json={"id_token": "x", "days": 14, "offset": 0})
    assert r.status_code == 200
    d = r.json()
    assert d["weight_delta"] == -1.0
    assert d["target_weight"] == 85.0
    assert d["target_kcal"] == 1800   # goals 未設定でも goal_profiles の計算値を使う
    assert len(d["labels"]) == 14 and len(d["weight"]) > 0


def test_trainer_detail_page_shares_weight_ui_and_nav():
    r = client.get(f"/trainer/members/{MEMBER}",
                   cookies={"session": auth.issue_session(TRAINER)})
    assert r.status_code == 200
    h = r.text
    for token in ('id="wCurrent"', 'id="wDelta"', 'id="wBaseDate"',
                  'id="wTarget"', 'id="wTargetWrap"', "＜ 過去", "?offset=1",
                  "/static/charts.js", "CalCharts.weight",
                  "CalCharts.weightText", "CalCharts.calorie", "目標体重"):
        assert token in h, token


def test_member_list_shows_goal_only_when_set():
    r = client.get("/trainer", cookies={"session": auth.issue_session(TRAINER)})
    assert r.status_code == 200
    assert "目的" in r.text and "減量" in r.text and "減塩" in r.text


def test_gym_bi_page_and_access():
    r = client.get(f"/gym/bi?gym_id={GID}",
                   cookies={"session": auth.issue_session(ADMIN)})
    assert r.status_code == 200
    for token in ("目的設定の内訳", "トレーナーごとの減量実績",
                  "会員ごとの体重推移", "gymBiPie", "gymBiBar",
                  "/static/charts.js", "初回の体重記録"):
        assert token in r.text, token
    r2 = client.get("/gym/bi", cookies={"session": auth.issue_session(TRAINER)})
    assert r2.status_code == 403


def test_day_detail_balance_semantics_in_template():
    src = (ROOT / "app/templates/day_detail.html").read_text(encoding="utf-8")
    # 収支＝摂取−消費、目標との差＝摂取−目標
    assert "(d.intake_kcal||0) - (d.burn_kcal||0)" in src
    assert "(d.intake_kcal||0) - d.target_kcal" in src
    # 目標線は摂取バーの行の中（摂取バー → 目標線 の順）
    i_bar = src.index('id="balIntakeBar"')
    i_gl = src.index('id="balGoalLine"')
    i_burn = src.index('id="balBurnBar"')
    assert i_bar < i_gl < i_burn
    # 符号付き表記（食べ過ぎは「＋」）
    assert "signedKcal" in src or "signKcal" in src
    # 食べ過ぎ（プラス）＝赤 / 余裕（マイナス）＝緑
    assert '_df > 0 ? "#ef4444" : "#10b981"' in src
    assert "目標との差（摂取－目標）" in src
    # 消費バー側には目標線を置かない（goal 行は1つだけ）
    assert src.count('id="balGoalLine"') == 1


def test_shared_charts_module_used_in_both_views():
    m = (ROOT / "app/templates/member_home.html").read_text(encoding="utf-8")
    assert "CalCharts.weight" in m and "CalCharts.weightText" in m
    assert "CalCharts.calorie" in m and "/static/charts.js" in m
    js = (ROOT / "app/static/charts.js").read_text(encoding="utf-8")
    # 目標線は摂取側（カロリーグラフ）にだけ引く
    assert "目標摂取" in js
    assert "weightText" in js and "signedKcal" in js


def test_goal_diff_sign_convention_on_all_screens():
    """目標との差は「摂取−目標」で統一。食べ過ぎ＝プラス、色は赤。"""
    dd = (ROOT / "app/templates/day_detail.html").read_text(encoding="utf-8")
    mh = (ROOT / "app/templates/member_home.html").read_text(encoding="utf-8")
    st = (ROOT / "app/templates/staff_member_detail.html").read_text(encoding="utf-8")
    for src in (dd, mh, st):
        assert "目標との差（摂取－目標）" in src
        assert "目標との差（目標－摂取）" not in src
    assert "(d.intake_kcal||0) - d.target_kcal" in dd
    assert "_signed(_in - _tg)" in mh
    assert "((_in - _tg)|round(0)|int)" in st
    assert '_df > 0 ? "#ef4444" : "#10b981"' in dd
    assert '(_in - _tg) > 0 ? "#ef4444" : "#10b981"' in mh
    assert "'#ef4444' if _d > 0 else '#10b981'" in st
