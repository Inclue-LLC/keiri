"""環境変数ベースの設定。"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

DATA_DIR = Path(os.environ.get("KEIRI_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = Path(os.environ.get("KEIRI_DB_PATH", DATA_DIR / "keiri.db"))

# 振込手数料として許容する差額の上限（円）。請求額 - 入金額 がこの範囲なら「手数料差引の可能性あり」
FEE_TOLERANCE = int(os.environ.get("KEIRI_FEE_TOLERANCE", "880"))

# 入金日が「請求日 - N日」〜「支払期限 + M日」の範囲なら日付が妥当とみなす
DATE_WINDOW_BEFORE_ISSUE = int(os.environ.get("KEIRI_DATE_WINDOW_BEFORE_ISSUE", "7"))
DATE_WINDOW_AFTER_DUE = int(os.environ.get("KEIRI_DATE_WINDOW_AFTER_DUE", "45"))

# HTTPS 配下で運用する場合は 1 にする（Cookie に Secure 属性を付与）
COOKIE_SECURE = os.environ.get("KEIRI_COOKIE_SECURE", "0") == "1"
SESSION_MAX_AGE = int(os.environ.get("KEIRI_SESSION_MAX_AGE", str(60 * 60 * 8)))


def secret_key() -> str:
    """セッション署名鍵。環境変数がなければデータディレクトリに生成して永続化する。"""
    key = os.environ.get("KEIRI_SECRET_KEY")
    if key:
        return key
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / "secret_key"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(48))
        path.chmod(0o600)
    return path.read_text().strip()
