"""Phase 10: ジムBI / トレーナーBI / AIコメント.

- ジムBI: 継続率・離脱予兆・目標達成率・トレーナーランキング・
  コメント相関・週次トレンド・記録の質
- トレーナーBI: 担当会員だけの継続率・離脱予兆・記録漏れ・摂取−目標
- AIコメント: 集計値だけで生成。未生成時は生成ボタン、失敗時は定型文へ
"""
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DB_PATH", str(Path(tempfile.mkdtemp()) / "phase10.db"))
os.environ.setdefault("TURSO_DATABASE_URL", "")

from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services import bi, db, goals, gym_db, insights  # noqa: E402
from app.services.dates import today_jst_date  # noqa: E402

ADMIN = "Up10_admin"
TRAINER = "Up10_tr"
MEMBER = "Up10_mem"
MEMBER2 = "Up10_mem2"
GID = 1
client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _db():
    settings.DB_PATH = str(Path(tempfile.mkdtemp()) / "gym_phase10.db")
    settings.ADMIN_USER_IDS = ADMIN
    db.init_db()
    yield


def _day(offset: int) -> str:
    return (today_jst_date() - timedelta(days=offset)).isoformat()


@pytest.fixture(autouse=True)
def _seed(_db):
    global GID
    gyms = gym_db.list_admin_gyms(ADMIN)
    GID = int(gyms[0]["gym_id"]) if gyms else int(
        gym_db.create_gym(name="Phase10ジム", owner_user_id=ADMIN)["id"])
    with db.get_conn() as c:
        for uid, nm in ((ADMIN, "管理者"), (TRAINER, "担当トレーナー"),
                        (MEMBER, "会員A"), (MEMBER2, "会員B")):
            c.execute("INSERT OR REPLACE INTO users (line_user_id, display_name)"
                      " VALUES (?,?)", (uid, nm))
        for uid, role in ((ADMIN, "gym_admin"), (TRAINER, "trainer")):
            c.execute("INSERT OR IGNORE INTO memberships (gym_id,user_id,role,status)"
                      " VALUES (?,?,?,'active')", (GID, uid, role))
        # 会員行（role=member）は再シード時に作り直す
        c.execute("DELETE FROM memberships WHERE user_id IN (?,?)",
                  (MEMBER, MEMBER2))
        c.execute("INSERT OR REPLACE INTO memberships"
                  " (gym_id,user_id,role,trainer_id,status)"
                  " VALUES (?,?,'member',?,'active')", (GID, MEMBER, TRAINER))
        c.execute("INSERT OR REPLACE INTO memberships"
                  " (gym_id,user_id,role,trainer_id,status)"
                  " VALUES (?,?,'member',?,'active')", (GID, MEMBER2, TRAINER))

        c.execute("DELETE FROM entries WHERE user_id IN (?,?)", (MEMBER, MEMBER2))
        c.execute("DELETE FROM weight_logs WHERE user_id IN (?,?)", (MEMBER, MEMBER2))
        c.execute("DELETE FROM goal_profiles WHERE user_id IN (?,?)",
                  (MEMBER, MEMBER2))

        # 会員A: 直近5日記録（今日を含む）＋ 目標摂取
        for off in range(0, 5):
            c.execute(
                "INSERT OR REPLACE INTO entries (user_id,date,meal_slot,food_name,kcal,"
                "protein_g,fat_g,carb_g,salt_g,source_type,confidence)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (MEMBER, _day(off), "breakfast", f"朝食{off}", 600, 20, 15, 80, 1.2,
                 "llm_estimate", "estimated"))
        c.execute("INSERT OR REPLACE INTO entries (user_id,date,meal_slot,food_name,kcal,"
                  "protein_g,fat_g,carb_g,salt_g,source_type,confidence)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (MEMBER, _day(0), "lunch", "昼食", 800, 30, 20, 90, 1.5,
                   "ocr_label", "confirmed"))
        c.execute("INSERT OR REPLACE INTO goals (user_id,date,target_kcal) VALUES (?,?,?)",
                  (MEMBER, _day(0), 1600.0))

        # 会員B: 8日前に記録が停止
        c.execute("INSERT OR REPLACE INTO entries (user_id,date,meal_slot,food_name,kcal,"
                  "protein_g,fat_g,carb_g,salt_g,source_type,confidence)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (MEMBER2, _day(8), "dinner", "夕食", 900, 25, 25, 100, 2.0,
                   "llm_estimate", "estimated"))

        # 目的: 会員A=減量 / 会員B=減塩
        c.execute("INSERT OR REPLACE INTO goal_profiles"
                  " (user_id,goal_mode,target_weight_kg,salt_target_g,"
                  "  protein_target_g,calc_target_kcal)"
                  " VALUES (?,?,?,?,?,?)", (MEMBER, "weight", 70.0, None, None, 1600))
        c.execute("INSERT OR REPLACE INTO goal_profiles"
                  " (user_id,goal_mode,target_weight_kg,salt_target_g,"
                  "  protein_target_g,calc_target_kcal)"
                  " VALUES (?,?,?,?,?,?)", (MEMBER2, "salt", None, 6.0, None, 1800))

        # 体重: 会員A は 75.0 → 72.5 で減量（実測）
        for off, w in ((20, 75.0), (10, 73.8), (1, 72.5)):
            c.execute("INSERT OR REPLACE INTO weight_logs"
                      " (user_id,date,weight_kg,is_measured) VALUES (?,?,?,1)",
                      (MEMBER, _day(off), w))
        c.execute("INSERT OR REPLACE INTO weight_logs"
                  " (user_id,date,weight_kg,is_measured) VALUES (?,?,?,0)",
                  (MEMBER, _day(5), 74.0))

        # トレーナーのコメント（30日内）
        c.execute("DELETE FROM comments WHERE user_id IN (?,?)", (MEMBER, MEMBER2))
        c.execute("INSERT OR REPLACE INTO comments (user_id,author_type,author_id,body,"
                  "target_date,is_directive) VALUES (?,?,?,?,?,0)",
                  (MEMBER, "trainer", TRAINER, "いい調子です", _day(1)))
    yield


# ---- 集計ロジック ----

def test_continuity_report_counts():
    cont = bi.continuity_report(GID, days=30)
    assert cont["members"] == 2
    assert cont["light"] == 1          # 会員Aのみ直近7日に記録
    assert cont["light_pct"] == 50.0
    assert cont["entry_count"] >= 6
    a = [r for r in cont["rows"] if r["user_id"] == MEMBER][0]
    b = [r for r in cont["rows"] if r["user_id"] == MEMBER2][0]
    assert a["days7"] >= 5 and a["light"] is True and a["settled"] is True
    assert b["light"] is False and b["idle_days"] == 8


def test_churn_risk_levels():
    churn = bi.churn_risk(GID, days=30)
    ids = {c["user_id"]: c for c in churn}
    assert MEMBER not in ids                       # 今日記録あり → 予兆なし
    assert ids[MEMBER2]["level"] == "red"          # 8日経過 → 赤
    assert ids[MEMBER2]["level_label"] == "赤"


def test_goal_achievement_summary():
    achv = bi.goal_achievement(GID, days=90)
    assert set(achv["summary"].keys()) == {"weight", "salt", "muscle"}
    w = achv["summary"]["weight"]
    assert w["members"] == 1 and w["achieved"] == 0   # 72.5 > 70.0 なので未達
    s = achv["summary"]["salt"]
    assert s["members"] == 1
    rows = {r["user_id"]: r for r in achv["rows"]}
    assert rows[MEMBER]["rate"] is not None
    assert rows[MEMBER2]["value"] is not None         # 何日達成できたか


def test_trainer_ranking_and_correlation():
    rank = bi.trainer_ranking(GID, days=90)
    assert rank and rank[0]["trainer_id"] == TRAINER
    assert rank[0]["members"] == 2
    assert rank[0]["light_pct"] == 50.0
    assert rank[0]["comments_30d"] >= 1
    assert rank[0]["avg_loss"] == 2.5                 # 75.0 → 72.5
    corr = bi.comment_correlation(GID, days=30)
    assert corr and corr[0]["comments_30d"] >= 1
    assert "achieved_pct" in corr[0]


def test_weekly_trend_and_quality():
    trend = bi.weekly_trend(GID, weeks=8)
    assert len(trend) == 8
    assert all("label" in t for t in trend)
    assert trend[-1]["members"] >= 1                  # 直近週に会員A
    q = bi.data_quality(GID, days=90)
    assert q["total_entries"] >= 6
    assert q["measured"] == 3 and q["weight_records"] == 4
    assert q["measured_pct"] == 75.0
    assert "テキスト推定" in q["source_text"]


def test_trainer_overview_scope():
    ov = bi.trainer_overview(TRAINER, days=30)
    assert ov["total"] == 2
    assert ov["light"] == 1
    assert ov["light_pct"] == 50.0
    assert ov["unread"] >= 0
    assert any(c["user_id"] == MEMBER2 for c in ov["churn"])
    # 今日の記録がない会員は会員B
    assert "会員B" in ov["no_record_today"]
    # 摂取−目標（会員A: 1400 − 1600 = −200）
    a = [r for r in ov["rows"] if r["user_id"] == MEMBER][0]
    assert a["today_intake"] == 1400 and a["target_kcal"] == 1600
    assert a["diff"] == -200
    assert ov["diff"]["under"] == 1


# ---- AIコメント ----

def test_ai_report_not_generated_by_default():
    r = insights.gym_report(GID, {}, period="week", allow_generate=False)
    assert r["available"] is False
    assert r["period_key"]
    assert r["text"] == ""


def test_ai_report_fallback_text_has_numbers():
    facts = {"会員数": 2, "7日記録あり": 1, "ライト継続率": "50.0%",
             "定着人数": 1, "定着率": "50.0%", "期間内の食事記録数": 6,
             "目的別人数": "減量 1名 / 減塩 1名", "離脱予兆(赤)": 1,
             "離脱予兆(黄)": 0, "未返信コメント数": 0,
             "コメント数トップ": "担当トレーナー（30日 1件）"}
    text = insights._gym_fallback(facts)
    for heading in ("【今週の傾向】", "【良かった点】", "【注意点】", "【来週やること】"):
        assert heading in text
    assert "50.0%" in text and "6件" in text


def test_trainer_suggest_fallback_has_three_lines():
    items = [{"user_id": MEMBER2, "name": "会員B", "経過日数": 8,
              "直近7日の記録日数": 0, "目標との差": None, "目的": "減塩"}]
    text = insights._trainer_fallback(items)
    assert "■ 会員B" in text
    assert "根拠:" in text and "提案:" in text and "文面:" in text


def test_trainer_suggest_not_generated_by_default():
    r = insights.trainer_suggest(TRAINER, [], allow_generate=False)
    assert r["available"] is False


def test_period_key_format():
    pk = insights.period_key("week")
    assert "-W" in pk and len(pk) == 8
    assert insights.period_key("month").count("-") == 1


# ---- 画面 ----

def _login(uid: str):
    return {"session": auth.issue_session(uid)}


def test_gym_bi_page_admin_ok_and_has_new_sections():
    r = client.get(f"/gym/bi?gym_id={GID}", cookies=_login(ADMIN))
    assert r.status_code == 200
    html = r.text
    for label in ("継続率（記録日ベース）", "離脱予兆", "目標達成率（目的別）",
                  "トレーナーランキング", "コメント数と成果の関係",
                  "週次トレンド", "記録の質", "AIジムレポート"):
        assert label in html, label
    assert "gymBiTrend" in html


def test_gym_bi_page_trainer_forbidden():
    r = client.get(f"/gym/bi?gym_id={GID}", cookies=_login(TRAINER))
    assert r.status_code == 403


def test_trainer_page_has_bi_and_ai_card():
    r = client.get("/trainer", cookies=_login(TRAINER))
    assert r.status_code == 200
    html = r.text
    assert "担当BI" in html
    assert "離脱予兆" in html
    assert "次の一手（AI）" in html
    assert "aiSuggestBtn" in html
    assert "今日の摂取と目標（摂取－目標）" in html


def test_ai_report_api_requires_gym_admin():
    r = client.post("/api/gym/ai-report", json={"gym_id": GID},
                    cookies=_login(TRAINER))
    assert r.status_code == 403


def test_ai_suggest_api_requires_staff():
    r = client.post("/api/trainer/ai-suggest", json={})
    assert r.status_code in (303, 403, 503)


def test_ai_suggest_api_returns_text_for_staff():
    r = client.post("/api/trainer/ai-suggest", json={}, cookies=_login(TRAINER))
    assert r.status_code == 200
    j = r.json()
    assert j.get("text")
    assert j.get("source") in ("ai", "rule", "cache")
