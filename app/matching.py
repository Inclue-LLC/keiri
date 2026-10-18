"""請求書と入金明細の照合ロジック。

照合は候補を提示するだけで、確定（matches への登録）は必ず人が画面から行う。
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import config, kana

STATUS_PAID = "入金済"
STATUS_REVIEW = "要確認"
STATUS_UNPAID = "未入金"
STATUS_OVERDUE = "期日超過"
STATUSES = [STATUS_PAID, STATUS_REVIEW, STATUS_UNPAID, STATUS_OVERDUE]

NAME_MATCH = 0.85
NAME_SIMILAR = 0.6


@dataclass
class Candidate:
    transaction: sqlite3.Row
    amount_diff: int  # 請求額 - 入金額（正なら入金が少ない）
    name_score: float
    matched_name: str
    date_ok: bool
    score: int = 0
    reasons: list[tuple[str, str]] = field(default_factory=list)  # (ラベル, 種別: ok/warn/ng)

    @property
    def fee_suspected(self) -> bool:
        return self.amount_diff > 0


@dataclass
class InvoiceView:
    invoice: sqlite3.Row
    status: str
    candidates: list[Candidate]
    match: sqlite3.Row | None


def name_keys(client: sqlite3.Row, aliases: list[sqlite3.Row]) -> list[tuple[str, str]]:
    """取引先に対して照合に使う (表示名, 正規化名) の一覧。"""
    keys = []
    for raw in [client["name_kana"], client["name"], *(a["alias"] for a in aliases)]:
        n = kana.normalize(raw)
        if n and n not in (k[1] for k in keys):
            keys.append((raw, n))
    return keys


def _date_ok(invoice: sqlite3.Row, tx_date: str) -> bool:
    d = date.fromisoformat(tx_date)
    issue = date.fromisoformat(invoice["issue_date"]) if invoice["issue_date"] else None
    due = date.fromisoformat(invoice["due_date"]) if invoice["due_date"] else None
    start = issue - timedelta(days=config.DATE_WINDOW_BEFORE_ISSUE) if issue else None
    if due:
        end = due + timedelta(days=config.DATE_WINDOW_AFTER_DUE)
    elif issue:
        end = issue + timedelta(days=90 + config.DATE_WINDOW_AFTER_DUE)
    else:
        end = None
    return (start is None or d >= start) and (end is None or d <= end)


def evaluate(invoice: sqlite3.Row, tx: sqlite3.Row, keys: list[tuple[str, str]]) -> Candidate | None:
    """1件の請求書と入金を比較し、候補になるなら Candidate を返す。"""
    diff = invoice["amount"] - tx["amount"]
    if diff < 0 or diff > config.FEE_TOLERANCE:
        return None
    payer = kana.normalize(tx["payer_kana"])
    best_name, best_score = "", 0.0
    for raw, n in keys:
        s = kana.similarity(payer, n)
        if s > best_score:
            best_name, best_score = raw, s
    date_ok = _date_ok(invoice, tx["date"])

    # 金額が近くても名義も日付も合わないものはノイズになるので候補にしない
    if best_score < NAME_SIMILAR and not date_ok:
        return None
    if diff > 0 and best_score < NAME_SIMILAR:
        return None

    c = Candidate(tx, diff, best_score, best_name, date_ok)
    if diff == 0:
        c.score += 50
        c.reasons.append(("金額一致", "ok"))
    else:
        c.score += 30
        c.reasons.append((f"手数料差引の可能性あり（差額 {diff:,}円）", "warn"))
    if best_score >= NAME_MATCH:
        c.score += 35
        c.reasons.append((f"名義一致（{best_name}）", "ok"))
    elif best_score >= NAME_SIMILAR:
        c.score += 20
        c.reasons.append((f"名義類似（{best_name}・{best_score:.0%}）", "warn"))
    else:
        c.reasons.append(("名義不一致", "ng"))
    if date_ok:
        c.score += 15
        c.reasons.append(("日付妥当", "ok"))
    else:
        c.reasons.append(("日付が期間外", "ng"))
    return c


def _load_aliases(conn: sqlite3.Connection) -> dict[int, list[sqlite3.Row]]:
    by_client = defaultdict(list)
    for a in conn.execute("SELECT * FROM client_aliases"):
        by_client[a["client_id"]].append(a)
    return by_client


def build_views(conn: sqlite3.Connection, today: date | None = None) -> list[InvoiceView]:
    """全請求書のステータスと候補を計算する。"""
    today = today or date.today()
    invoices = conn.execute(
        """SELECT i.*, c.name AS client_name, c.name_kana AS client_kana
           FROM invoices i JOIN clients c ON c.id = i.client_id
           ORDER BY COALESCE(i.due_date, i.issue_date) DESC, i.invoice_no DESC"""
    ).fetchall()
    clients = {c["id"]: c for c in conn.execute("SELECT * FROM clients")}
    aliases = _load_aliases(conn)
    matches = {m["invoice_id"]: m for m in conn.execute("SELECT * FROM matches")}
    used_tx = {m["transaction_id"] for m in matches.values() if m["transaction_id"]}
    open_tx = [t for t in conn.execute("SELECT * FROM transactions ORDER BY date") if t["id"] not in used_tx]

    # 金額で絞り込んでから名義比較する
    by_amount = defaultdict(list)
    for t in open_tx:
        by_amount[t["amount"]].append(t)

    views = []
    for inv in invoices:
        match = matches.get(inv["id"])
        if match:
            views.append(InvoiceView(inv, STATUS_PAID, [], match))
            continue
        keys = name_keys(clients[inv["client_id"]], aliases[inv["client_id"]])
        cands = []
        for amt in range(inv["amount"] - config.FEE_TOLERANCE, inv["amount"] + 1):
            for t in by_amount.get(amt, ()):
                c = evaluate(inv, t, keys)
                if c:
                    cands.append(c)
        cands.sort(key=lambda c: (-c.score, c.transaction["date"]))
        if cands:
            status = STATUS_REVIEW
        elif inv["due_date"] and date.fromisoformat(inv["due_date"]) < today:
            status = STATUS_OVERDUE
        else:
            status = STATUS_UNPAID
        views.append(InvoiceView(inv, status, cands, None))
    return views
