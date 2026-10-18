#!/bin/bash
# 初回セットアップ（仮想環境の作成・ライブラリのインストール・最初のユーザー作成）と起動をまとめて行う
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "初回セットアップ中..."
  python3 -m venv .venv
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt
fi

USERS=$(.venv/bin/python -c "from app import repo; from app.main import make_store; print(len(repo.list_users(make_store())))")
if [ "$USERS" = "0" ]; then
  echo "最初のユーザーを作成します。"
  read -r -p "ユーザー名: " NAME
  .venv/bin/python -m app.cli create-user "$NAME"
fi

PORT="${PORT:-8000}"
echo "起動しました → http://127.0.0.1:${PORT}  （終了は Ctrl + C）"
(sleep 2 && command -v open >/dev/null && open "http://127.0.0.1:${PORT}") &
exec .venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port "$PORT"
