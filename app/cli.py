"""管理用コマンド（パスワード方式のとき）。

    python -m app.cli create-user <ユーザー名>
    python -m app.cli reset-password <ユーザー名>
"""
from __future__ import annotations

import argparse
import getpass
import sys

from . import auth, repo
from .main import make_store


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

    store = make_store()
    pw_hash = auth.hash_password(_read_password())
    if args.command == "create-user":
        if not repo.create_user(store, args.username, pw_hash):
            print(f"ユーザー {args.username} は既に存在します", file=sys.stderr)
            return 1
        repo.audit(store, None, "user_create", f"{args.username}（CLI）")
    else:
        user = repo.find_user(store, args.username)
        if not user:
            print(f"ユーザー {args.username} が見つかりません", file=sys.stderr)
            return 1
        repo.set_password(store, user["id"], pw_hash)
        repo.audit(store, None, "password_reset", f"{args.username}（CLI）")
    print("完了しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
