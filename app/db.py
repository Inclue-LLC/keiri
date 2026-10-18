"""SQLite スキーマと接続ヘルパー。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    name_kana TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 取引先ごとの振込名義の別名
CREATE TABLE IF NOT EXISTS client_aliases (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    alias_norm TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (client_id, alias_norm)
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY,
    invoice_no TEXT NOT NULL UNIQUE,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    subject TEXT NOT NULL DEFAULT '',
    issue_date TEXT,
    due_date TEXT,
    amount INTEGER NOT NULL,
    memo TEXT NOT NULL DEFAULT '',
    imported_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 入金明細（入金のみ取り込む）
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    bank TEXT NOT NULL,
    date TEXT NOT NULL,
    amount INTEGER NOT NULL,
    description TEXT NOT NULL,
    payer_kana TEXT NOT NULL,
    balance INTEGER,
    dedupe_key TEXT NOT NULL UNIQUE,
    imported_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 人が確定した消込。transaction_id が NULL の場合は明細なしの手動入金済。
-- まとめ入金では同じ transaction_id に複数の請求書が紐づく
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL UNIQUE REFERENCES invoices(id),
    transaction_id INTEGER REFERENCES transactions(id),
    fee_diff INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    confirmed_by INTEGER NOT NULL REFERENCES users(id),
    confirmed_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_matches_transaction ON matches (transaction_id);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    filename TEXT NOT NULL,
    added INTEGER NOT NULL,
    skipped INTEGER NOT NULL,
    imported_by INTEGER NOT NULL REFERENCES users(id),
    imported_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path | None = None) -> None:
    conn = connect(path)
    try:
        _migrate_matches(conn)
        conn.executescript(SCHEMA)
        # 既存 DB への列追加
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(invoices)")}
        if "memo" not in cols:
            conn.execute("ALTER TABLE invoices ADD COLUMN memo TEXT NOT NULL DEFAULT ''")
        conn.commit()
    finally:
        conn.close()


def _migrate_matches(conn: sqlite3.Connection) -> None:
    """旧スキーマ（1入金=1請求書の UNIQUE 制約あり）の matches テーブルを作り直す。"""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'matches'").fetchone()
    if not row or "transaction_id INTEGER UNIQUE" not in row["sql"]:
        return
    conn.executescript("""
        ALTER TABLE matches RENAME TO matches_old;
        CREATE TABLE matches (
            id INTEGER PRIMARY KEY,
            invoice_id INTEGER NOT NULL UNIQUE REFERENCES invoices(id),
            transaction_id INTEGER REFERENCES transactions(id),
            fee_diff INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            confirmed_by INTEGER NOT NULL REFERENCES users(id),
            confirmed_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        INSERT INTO matches SELECT * FROM matches_old;
        DROP TABLE matches_old;
    """)


@contextmanager
def transaction(path: Path | None = None):
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def audit(conn: sqlite3.Connection, user_id: int | None, action: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_log (user_id, action, detail) VALUES (?, ?, ?)",
        (user_id, action, detail),
    )
