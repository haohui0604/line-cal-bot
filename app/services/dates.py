"""JST(UTC+9)固定の日付ユーティリティ.

Render等のサーバーはUTCで動くため、date.today() を直接使うと
日本時間の 0:00〜8:59 が「前日」として記録されてしまう。
日付取得は必ずここの関数を使うこと。
"""
from datetime import datetime, timedelta, timezone, date as _date

JST = timezone(timedelta(hours=9), name="JST")


def now_jst() -> datetime:
    return datetime.now(JST)


def today_jst() -> str:
    """今日の日付 (YYYY-MM-DD, JST基準)."""
    return now_jst().date().isoformat()


def today_jst_date() -> _date:
    """今日の日付 (date オブジェクト, JST基準)."""
    return now_jst().date()
