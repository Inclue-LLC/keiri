"""請求書と入金明細の照合ロジック。

照合は候補を提示するだけで、確定（matches への登録）は必ず人が画面から行う。
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from itertools import combinations
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

# まとめ入金（1回の入金で複数の請求書を支払い）の候補を探す範囲
COMBINE_MAX_INVOICES = 4
COMBINE_POOL_SIZE = 12


@dataclass
class Candidate:
    transaction: sqlite3.Row
    amount_diff: int  # 請求額 - 入金額（正なら入金が少ない）
    name_score: float
    matched_name: str
    date_ok: bool
    score: int = 0
    reasons: list[tuple[str, str]] = field(default_factory=list)  # (ラベル, 種別: ok/warn/ng)
    # まとめ入金の場合、合計が入金額になる請求書の組（自分を含む）。単独の場合は空
    group: list[sqlite3.Row] = field(default_factory=list)
    days_from_due: int = 0
    invoice_id: int = 0

    @property
    def fee_suspected(self) -> bool:
        return self.amount_diff > 0

    @property
    def is_combined(self) -> bool:
        return len(self.group) > 1


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


def name_score(payer_kana: str, keys: list[tuple[str, str]]) -> tuple[str, float]:
    """振込名義と取引先の名義候補を比べ、最も近いものの (表示名, 類似度) を返す。"""
    payer = kana.normalize(payer_kana)
    best_name, best_score = "", 0.0
    for raw, n in keys:
        s = kana.similarity(payer, n)
        if s > best_score:
            best_name, best_score = raw, s
    return best_name, best_score


def _days_from_due(invoice: sqlite3.Row, tx_date: str) -> int:
    base = invoice["due_date"] or invoice["issue_date"]
    if not base:
        return 0
    return abs((date.fromisoformat(tx_date) - date.fromisoformat(base)).days)


def _add_name_and_date_reasons(c: Candidate) -> None:
    if c.name_score >= NAME_MATCH:
        c.score += 35
        c.reasons.append((f"名義一致（{c.matched_name}）", "ok"))
    elif c.name_score >= NAME_SIMILAR:
        c.score += 20
        c.reasons.append((f"名義類似（{c.matched_name}・{c.name_score:.0%}）", "warn"))
    else:
        c.reasons.append(("名義不一致", "ng"))
    if c.date_ok:
        c.score += 15
        c.reasons.append(("日付妥当", "ok"))
    else:
        c.reasons.append(("日付が期間外", "ng"))


def evaluate(invoice: sqlite3.Row, tx: sqlite3.Row, keys: list[tuple[str, str]]) -> Candidate | None:
    """1件の請求書と入金を比較し、候補になるなら Candidate を返す。"""
    diff = invoice["amount"] - tx["amount"]
    if diff < 0 or diff > config.FEE_TOLERANCE:
        return None
    best_name, best_score = name_score(tx["payer_kana"], keys)
    date_ok = _date_ok(invoice, tx["date"])

    # 金額が近くても名義も日付も合わないものはノイズになるので候補にしない
    if best_score < NAME_SIMILAR and not date_ok:
        return None
    # 差額がある候補は誤検知が多いので、名義と日付の両方が合う場合に限る
    if diff > 0 and (best_score < NAME_SIMILAR or not date_ok):
        return None

    c = Candidate(
        tx, diff, best_score, best_name, date_ok,
        days_from_due=_days_from_due(invoice, tx["date"]), invoice_id=invoice["id"],
    )
    if diff == 0:
        c.score += 50
        c.reasons.append(("金額一致", "ok"))
    else:
        c.score += 30
        c.reasons.append((f"手数料差引の可能性あり（差額 {diff:,}円）", "warn"))
    _add_name_and_date_reasons(c)
    return c


def combined_candidates(
    tx: sqlite3.Row, invoices: list[sqlite3.Row], keys: list[tuple[str, str]]
) -> list[Candidate]:
    """1回の入金が同じ取引先の複数請求書の合計（手数料差引を含む）になっていないか探す。

    名義が類似以上の取引先に限り、入金日が妥当な期間にある未消込の請求書から組み合わせる。
    """
    best_name, best_score = name_score(tx["payer_kana"], keys)
    if best_score < NAME_SIMILAR:
        return []
    pool = [i for i in invoices if i["amount"] <= tx["amount"] + config.FEE_TOLERANCE and _date_ok(i, tx["date"])]
    # 入金日に支払期限が近い請求書を優先して候補の母集団を絞る
    pool.sort(key=lambda i: _days_from_due(i, tx["date"]))
    pool = pool[:COMBINE_POOL_SIZE]
    result = []
    for size in range(2, COMBINE_MAX_INVOICES + 1):
        for combo in combinations(pool, size):
            diff = sum(i["amount"] for i in combo) - tx["amount"]
            if not 0 <= diff <= config.FEE_TOLERANCE:
                continue
            group = sorted(combo, key=lambda i: i["invoice_no"])
            nos = " + ".join(i["invoice_no"] for i in group)
            for inv in group:
                c = Candidate(
                    tx, diff, best_score, best_name, True, group=group,
                    days_from_due=_days_from_due(inv, tx["date"]), invoice_id=inv["id"],
                )
                c.score += 40 if diff == 0 else 25
                c.reasons.append((f"まとめ入金の可能性（{nos}）", "warn"))
                if diff:
                    c.reasons.append((f"手数料差引の可能性あり（差額 {diff:,}円）", "warn"))
                _add_name_and_date_reasons(c)
                result.append(c)
    return result


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

    keys_by_client = {cid: name_keys(c, aliases[cid]) for cid, c in clients.items()}
    open_by_client = defaultdict(list)
    for inv in invoices:
        if inv["id"] not in matches:
            open_by_client[inv["client_id"]].append(inv)
    combined_by_invoice = defaultdict(list)
    for t in open_tx:
        for cid, invs in open_by_client.items():
            if len(invs) >= 2:
                for c in combined_candidates(t, invs, keys_by_client[cid]):
                    combined_by_invoice[c.invoice_id].append(c)

    views = []
    for inv in invoices:
        match = matches.get(inv["id"])
        if match:
            views.append(InvoiceView(inv, STATUS_PAID, [], match))
            continue
        keys = keys_by_client[inv["client_id"]]
        cands = []
        for amt in range(inv["amount"] - config.FEE_TOLERANCE, inv["amount"] + 1):
            for t in by_amount.get(amt, ()):
                c = evaluate(inv, t, keys)
                if c:
                    cands.append(c)
        cands += combined_by_invoice.get(inv["id"], [])
        # 同額の請求が毎月ある取引先に備え、同点なら支払期限に近い入金を先に出す
        cands.sort(key=lambda c: (-c.score, c.days_from_due, c.transaction["date"]))
        if cands:
            status = STATUS_REVIEW
        elif inv["due_date"] and date.fromisoformat(inv["due_date"]) < today:
            status = STATUS_OVERDUE
        else:
            status = STATUS_UNPAID
        views.append(InvoiceView(inv, status, cands, None))
    return views
