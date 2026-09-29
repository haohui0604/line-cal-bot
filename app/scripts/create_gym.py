"""PoC用: ジムを手動作成して入会コードを発行するスクリプト.

使い方:
    python scripts/create_gym.py "ジム名" <オーナーのLINE user ID>

例:
    python scripts/create_gym.py "ボディメイクジム渋谷" U1234abcd...

オーナーの LINE user ID は、オーナー本人がボットに一度話しかけたあと
users テーブル、または webhook のログから確認できる。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.db import init_db
from app.services.gym_db import upsert_user, create_gym


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    name, owner = sys.argv[1], sys.argv[2]
    init_db()
    upsert_user(owner)
    gym = create_gym(name=name, owner_user_id=owner)
    print(f"ジム作成完了: {gym['name']}")
    print(f"入会コード:   {gym['join_code']}  ← 会員はボットにこのコードを送る")
    print(f"gym_id:       {gym['id']}")


if __name__ == "__main__":
    main()
