"""前日/翌日ナビゲーションの回帰テスト (Phase 3.5d).

ブラウザ側の日付演算は Node で実行して検証する。
- 表示中の日付から 前日/翌日 が「ちょうど 1 日」だけ動くこと
- TZ を変えても結果が変わらないこと
- 旧実装(toISOString)が 1 日ずれていたことの再現
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests" / "day_nav_check.js"
TEMPLATE = ROOT / "app" / "templates" / "day_detail.html"
DAY_NAV = ROOT / "app" / "static" / "js" / "day_nav.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not available")


def _run(tz: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TZ"] = tz
    return subprocess.run(["node", str(SCRIPT)], cwd=str(ROOT),
                          capture_output=True, text=True, env=env, timeout=60)


@pytest.mark.parametrize("tz", ["UTC", "Asia/Tokyo"])
def test_nav_moves_exactly_one_day(tz):
    """前日/翌日が表示日からちょうど 1 日動く（タイムゾーン非依存）."""
    r = _run(tz)
    assert r.returncode == 0, f"TZ={tz}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    out = r.stdout
    assert "all assertions passed" in out
    # 実測値がログに出ていることを確認（証跡）
    assert "shiftDate('2026-09-28', -1) = 2026-09-27" in out
    assert "shiftDate('2026-09-28', +1) = 2026-09-29" in out
    assert "前日 x3 = 2026-09-25" in out
    assert "前日→翌日 = 2026-09-28" in out


def test_old_impl_bug_is_reproduced():
    """旧実装(toISOString)の 1 日ずれを再現して記録する."""
    r = _run("Asia/Tokyo")
    assert r.returncode == 0, r.stderr
    assert "旧実装 前日 = 2026-09-26" in r.stdout   # 2 日戻っていた
    assert "旧実装 翌日 = 2026-09-28" in r.stdout   # 動かなかった


def test_template_uses_daynav():
    """テンプレートが DayNav を使い、UTC 変換(toISOString)を残していないこと."""
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "day_nav.js" in html
    assert "DayNav.shiftDate" in html
    assert "toISOString" not in html


def test_day_nav_file_exists():
    assert DAY_NAV.exists(), "app/static/js/day_nav.js がありません"
