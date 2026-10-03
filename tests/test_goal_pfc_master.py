"""目標PFCマスタ（脂質・炭水化物）と、その目安計算の回帰テスト."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "pfc.db")
os.environ["TURSO_DATABASE_URL"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import db as dbm  # noqa: E402
from app.services import goals  # noqa: E402


def _run_migrations():
    for name in ("run_migrations", "apply_migrations", "init_db",
                 "ensure_migrations"):
        fn = getattr(dbm, name, None)
        if callable(fn):
            fn()
            return name
    raise AssertionError("migration runner が見つかりません")


def test_migration_adds_pfc_columns_idempotently():
    runner = _run_migrations()
    assert runner
    _run_migrations()  # 2回目でも落ちないこと（冪等）
    cols = goals._table_cols("goal_profiles")
    assert "fat_target_g" in cols
    assert "carb_target_g" in cols


def test_pfc_targets_derived_from_weight_and_kcal(monkeypatch):
    _run_migrations()
    monkeypatch.setattr(goals, "_latest_weight", lambda uid: 60.0)
    t = goals.pfc_targets("U-DERIVE", target_kcal=1800)
    assert t["protein_g"] == 96.0            # 60kg × 1.6
    assert t["fat_g"] == 50.0                # 1800 × 0.25 / 9
    rest = 1800 - 96.0 * 4 - 50.0 * 9        # 594 → /4
    assert t["carb_g"] == round(rest / 4, 1)


def test_save_pfc_targets_overrides_derivation(monkeypatch):
    _run_migrations()
    monkeypatch.setattr(goals, "_latest_weight", lambda uid: 60.0)
    goals.save_pfc_targets("U-OVR", fat_target_g=60.0, carb_target_g=200.0)
    t = goals.pfc_targets("U-OVR", target_kcal=1800)
    assert t["fat_g"] == 60.0
    assert t["carb_g"] == 200.0


def test_pfc_targets_tolerates_missing_columns(monkeypatch):
    """カラム追加前のDBでも落ちず、計算で補うこと."""
    monkeypatch.setattr(goals, "_table_cols", lambda t: {"goal_mode"})
    monkeypatch.setattr(goals, "_latest_weight", lambda uid: 60.0)
    t = goals.pfc_targets("U-OLD", target_kcal=1800)
    assert t["fat_g"] == 50.0
    assert t["carb_g"] is not None
    # 保存は何も壊さずスキップされる
    goals.save_pfc_targets("U-OLD", fat_target_g=60.0)


def test_context_line_includes_pfc_targets(monkeypatch):
    _run_migrations()
    monkeypatch.setattr(goals, "_latest_weight", lambda uid: 60.0)
    goals.save_profile("U-CTX", goal_mode="weight", target_weight_kg=60.0,
                       goal_days=90, calc_target_kcal=1800.0)
    line = goals.context_line("U-CTX")
    assert "目的: 減量" in line
    assert "目安PFC" in line
    assert "P96g" in line
