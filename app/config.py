"""環境変数ベースの設定。"""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("KEIRI_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))

# 保存先: file（ローカルの JSON ファイル）または firestore（Google Cloud）
STORE = os.environ.get("KEIRI_STORE", "file")
STORE_PATH = Path(os.environ.get("KEIRI_STORE_PATH", DATA_DIR / "keiri.json"))

# ログイン方式: password（ユーザー名とパスワード）または google（Google アカウント）
AUTH = os.environ.get("KEIRI_AUTH", "password")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
# Google ログインを許可するドメイン（例: inclue-llc.jp）。空なら制限しない
ALLOWED_DOMAIN = os.environ.get("KEIRI_ALLOWED_DOMAIN", "").strip().lower()
# さらに個別のメールアドレスに絞る場合はカンマ区切りで指定
ALLOWED_EMAILS = {e.strip().lower() for e in os.environ.get("KEIRI_ALLOWED_EMAILS", "").split(",") if e.strip()}

# 振込手数料として許容する差額の上限（円）。請求額 - 入金額 がこの範囲なら「手数料差引の可能性あり」
FEE_TOLERANCE = int(os.environ.get("KEIRI_FEE_TOLERANCE", "880"))

# 入金日が「請求日 - N日」〜「支払期限 + M日」の範囲なら日付が妥当とみなす
DATE_WINDOW_BEFORE_ISSUE = int(os.environ.get("KEIRI_DATE_WINDOW_BEFORE_ISSUE", "7"))
DATE_WINDOW_AFTER_DUE = int(os.environ.get("KEIRI_DATE_WINDOW_AFTER_DUE", "45"))

# HTTPS 配下で運用する場合は 1 にする（Cookie に Secure 属性を付与）
COOKIE_SECURE = os.environ.get("KEIRI_COOKIE_SECURE", "0") == "1"
SESSION_MAX_AGE = int(os.environ.get("KEIRI_SESSION_MAX_AGE", str(60 * 60 * 8)))
# セッション署名鍵。未設定なら保存先に生成して全インスタンスで共有する
SECRET_KEY = os.environ.get("KEIRI_SECRET_KEY", "")

# パスワード方式で、ユーザーが1人もいない場合に起動時に作成する初期ユーザー
INITIAL_USER = os.environ.get("KEIRI_INITIAL_USER", "")
INITIAL_PASSWORD = os.environ.get("KEIRI_INITIAL_PASSWORD", "")
