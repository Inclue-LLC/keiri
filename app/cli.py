"""管理用コマンド。

    python -m app.cli create-user <ユーザー名>
    python -m app.cli reset-password <ユーザー名>
"""
import argparse
import getpass
import sqlite3
import sys

from . import auth, db


def _read_password() -> str:
    while True:
        pw = getpass.getpass("パスワード: ")
        err = auth.validate_new_password(pw)
        if err:
            print(err, file=sys.stderr)
            continue
        if pw != getpass.getpass("パスワード（確認）: "):
            print("一致しません", file=sys.stderr)
            continue
        return pw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("create-user", "reset-password"):
        sub.add_parser(name).add_argument("username")
    args = parser.parse_args(argv)

    db.init_db()
    pw_hash = auth.hash_password(_read_password())
    with db.transaction() as conn:
        if args.command == "create-user":
            try:
                conn.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", (args.username, pw_hash))
            except sqlite3.IntegrityError:
                print(f"ユーザー {args.username} は既に存在します", file=sys.stderr)
                return 1
            db.audit(conn, None, "user_create", f"{args.username}（CLI）")
        else:
            cur = conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (pw_hash, args.username))
            if not cur.rowcount:
                print(f"ユーザー {args.username} が見つかりません", file=sys.stderr)
                return 1
            db.audit(conn, None, "password_reset", f"{args.username}（CLI）")
    print("完了しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
