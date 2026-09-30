"""テストは毎回まっさらな一時DBを使う（前回実行のデータが残ると誤判定するため）."""
import os
import pathlib
import tempfile

os.environ.pop("TURSO_DATABASE_URL", None)
os.environ.pop("TURSO_AUTH_TOKEN", None)
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="lcb-test-"))
os.environ["DB_PATH"] = str(_TMP / "test.db")
