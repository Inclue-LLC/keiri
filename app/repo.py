"""業務データの読み書き。保存先（store）の種類に依存しない形で書く。

コレクションとドキュメント ID:
    users         u+hash(ユーザー名またはメールアドレス)
    clients       c+hash(取引先名)
    aliases       a+hash(取引先ID・正規化した別名)   … 取引先ごとの振込名義の別名
    invoices      i+hash(請求書番号)
    transactions  明細の重複判定キー                 … invoice_ids に消込先の請求書 ID を持つ
    matches       請求書 ID                          … 人が確定した消込（transaction_id なしは明細なし確定）
    imports / audit_log  時刻順の ID
    settings      session（セッション署名鍵）
"""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import csv_import, kana
from .store import Conflict, Store, Txn

TZ = ZoneInfo("Asia/Tokyo")


def now() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now().date()


def _stamp() -> dict:
    """作成日時（表示用）と並び順用のタイムスタンプ。"""
    return {"created_at": now().strftime("%Y-%m-%d %H:%M:%S"), "ts": time.time()}


def _hash(prefix: str, value: str) -> str:
    return prefix + hashlib.sha256(value.encode()).hexdigest()[:20]


def new_id() -> str:
    return f"{time.time_ns():020d}{secrets.token_hex(3)}"


def user_id_for(username: str) -> str:
    return _hash("u", username.strip().lower())


def client_id_for(name: str) -> str:
    return _hash("c", name)


def invoice_id_for(invoice_no: str) -> str:
    return _hash("i", invoice_no)


def alias_id_for(client_id: str, alias: str) -> str:
    return _hash("a", f"{client_id}|{kana.normalize(alias)}")


def transaction_id_for(dedupe_key: str) -> str:
    return "t" + dedupe_key[:32]


def latest(store: Store, coll: str, limit: int) -> list[dict]:
    return sorted(store.all(coll), key=lambda d: d.get("ts", 0), reverse=True)[:limit]


def audit(target: Store | Txn, user_id: str | None, action: str, detail: str = "") -> None:
    target.set("audit_log", new_id(), {"user_id": user_id, "action": action, "detail": detail, **_stamp()})


# ---------------------------------------------------------------- ユーザー

def get_user(store: Store, user_id: str) -> dict | None:
    return store.get("users", user_id)


def find_user(store: Store, username: str) -> dict | None:
    return store.get("users", user_id_for(username))


def list_users(store: Store) -> list[dict]:
    return sorted(store.all("users"), key=lambda u: u.get("ts", 0))


def create_user(store: Store, username: str, password_hash: str | None) -> bool:
    return store.create(
        "users", user_id_for(username), {"username": username.strip(), "password_hash": password_hash, **_stamp()}
    )


def ensure_user(store: Store, username: str) -> dict:
    """Google ログインのユーザーを初回ログイン時に登録する。"""
    if create_user(store, username, None):
        audit(store, user_id_for(username), "user_create", f"{username}（初回ログイン）")
    return find_user(store, username)


def set_password(store: Store, user_id: str, password_hash: str) -> None:
    store.update("users", user_id, {"password_hash": password_hash})


def session_secret(store: Store) -> str:
    """セッション署名鍵。初回に生成して保存し、全インスタンスで共有する。"""
    store.create("settings", "session", {"secret": secrets.token_urlsafe(48)})
    return store.get("settings", "session")["secret"]


# ---------------------------------------------------------------- 全体の読み込み

@dataclass
class Snapshot:
    invoices: list[dict]                    # client_name / client_kana を付与済み
    clients: dict[str, dict]
    aliases: dict[str, list[dict]]          # 取引先 ID → 別名
    matches: dict[str, dict]                # 請求書 ID → 消込
    transactions: list[dict]                # 入金日順
    users: dict[str, dict] = field(default_factory=dict)

    @property
    def invoices_by_id(self) -> dict[str, dict]:
        return {i["id"]: i for i in self.invoices}


def load(store: Store) -> Snapshot:
    clients = {c["id"]: c for c in store.all("clients")}
    aliases: dict[str, list[dict]] = {cid: [] for cid in clients}
    for a in sorted(store.all("aliases"), key=lambda a: a.get("ts", 0)):
        aliases.setdefault(a["client_id"], []).append(a)
    invoices = []
    for inv in store.all("invoices"):
        c = clients.get(inv["client_id"], {})
        invoices.append({**inv, "client_name": c.get("name", ""), "client_kana": c.get("name_kana", "")})
    invoices.sort(key=lambda i: (i.get("due_date") or i.get("issue_date") or "", i["invoice_no"]), reverse=True)
    return Snapshot(
        invoices=invoices,
        clients=clients,
        aliases=aliases,
        matches={m["id"]: m for m in store.all("matches")},
        transactions=sorted(store.all("transactions"), key=lambda t: (t["date"], t["id"])),
        users={u["id"]: u for u in store.all("users")},
    )


# ---------------------------------------------------------------- 取込

def import_invoices(store: Store, parsed: csv_import.ParseResult) -> tuple[int, int, int]:
    existing = {i["id"]: i for i in store.all("invoices")}
    matched = {m["id"] for m in store.all("matches")}
    clients = {c["id"]: c for c in store.all("clients")}
    client_writes: dict[str, dict] = {}
    invoice_writes: dict[str, dict] = {}
    added = updated = skipped = 0
    for inv in parsed.rows:
        cid = client_id_for(inv.client_name)
        client = client_writes.get(cid) or clients.get(cid)
        if client is None:
            client = {"name": inv.client_name, "name_kana": inv.client_kana, **_stamp()}
            client_writes[cid] = client
        elif inv.client_kana and not client.get("name_kana"):
            client_writes[cid] = {**client, "name_kana": inv.client_kana}

        iid = invoice_id_for(inv.invoice_no)
        fields = {
            "invoice_no": inv.invoice_no, "client_id": cid, "subject": inv.subject, "issue_date": inv.issue_date,
            "due_date": inv.due_date, "amount": inv.amount, "memo": inv.memo,
        }
        if iid not in existing:
            invoice_writes[iid] = {**fields, **_stamp()}
            added += 1
        elif iid not in matched:
            invoice_writes[iid] = {**existing[iid], **fields}
            updated += 1
        else:
            # 確定済みの請求書は上書きしない
            skipped += 1
    if client_writes:
        store.set_many("clients", client_writes)
    if invoice_writes:
        store.set_many("invoices", invoice_writes)
    return added, updated, skipped


def import_transactions(store: Store, parsed: csv_import.ParseResult) -> tuple[int, int]:
    rows = {transaction_id_for(t.dedupe_key): t for t in parsed.rows}
    existing = store.get_many("transactions", list(rows))
    new = {
        tid: {
            "bank": t.bank, "date": t.date, "amount": t.amount, "description": t.description,
            "payer_kana": t.payer_kana, "balance": t.balance, "invoice_ids": [], **_stamp(),
        }
        for tid, t in rows.items() if tid not in existing
    }
    if new:
        store.set_many("transactions", new)
    return len(new), len(parsed.rows) - len(new)


def record_import(store: Store, user_id: str, kind: str, filename: str, added: int, skipped: int, msg: str) -> None:
    store.set("imports", new_id(), {
        "kind": kind, "filename": filename, "added": added, "skipped": skipped, "imported_by": user_id, **_stamp(),
    })
    audit(store, user_id, "import", f"{kind} {filename}: {msg}")


# ---------------------------------------------------------------- 消込

class ConfirmError(Exception):
    pass


def confirm(
    store: Store, invoice_ids: list[str], transaction_id: str, note: str, user_id: str, register_alias: bool
) -> tuple[list[dict], dict]:
    """入金を請求書（まとめ入金なら複数）に消し込む。二重消込を防ぐためトランザクションで行う。"""

    def fn(txn: Txn):
        invs = [txn.get("invoices", i) for i in invoice_ids]
        tx = txn.get("transactions", transaction_id)
        if not all(invs) or not tx:
            raise ConfirmError("請求書または入金明細が見つかりません")
        if tx.get("invoice_ids") or any(txn.get("matches", i) for i in invoice_ids):
            raise ConfirmError("この請求書または入金は既に消込済です")
        alias_id = alias_id_for(invs[0]["client_id"], tx["payer_kana"])
        add_alias = register_alias and kana.normalize(tx["payer_kana"]) and not txn.get("aliases", alias_id)

        fee_diff = sum(i["amount"] for i in invs) - tx["amount"]
        stamp = _stamp()
        for n, inv in enumerate(invs):
            # まとめ入金の差額（手数料）は先頭の請求書にだけ記録する
            txn.create("matches", inv["id"], {
                "invoice_id": inv["id"], "transaction_id": transaction_id, "fee_diff": fee_diff if n == 0 else 0,
                "note": note, "confirmed_by": user_id, "confirmed_at": stamp["created_at"], **stamp,
            })
        txn.update("transactions", transaction_id, {"invoice_ids": [i["id"] for i in invs]})
        if add_alias:
            txn.set("aliases", alias_id, _alias_doc(invs[0]["client_id"], tx["payer_kana"]))
        audit(
            txn, user_id, "confirm",
            f"請求書 {' + '.join(i['invoice_no'] for i in invs)} ← 入金 {tx['date']} {tx['amount']:,}円 {tx['payer_kana']}",
        )
        return invs, tx

    try:
        return store.run_transaction(fn)
    except Conflict as e:
        raise ConfirmError("この請求書または入金は既に消込済です") from e


def confirm_manual(store: Store, invoice_id: str, note: str, user_id: str) -> dict:
    def fn(txn: Txn):
        inv = txn.get("invoices", invoice_id)
        if not inv:
            raise ConfirmError("請求書が見つかりません")
        if txn.get("matches", invoice_id):
            raise ConfirmError("この請求書は既に消込済です")
        stamp = _stamp()
        txn.create("matches", invoice_id, {
            "invoice_id": invoice_id, "transaction_id": None, "fee_diff": 0, "note": note,
            "confirmed_by": user_id, "confirmed_at": stamp["created_at"], **stamp,
        })
        audit(txn, user_id, "confirm_manual", f"請求書 {inv['invoice_no']}: {note}")
        return inv

    try:
        return store.run_transaction(fn)
    except Conflict as e:
        raise ConfirmError("この請求書は既に消込済です") from e


def unconfirm(store: Store, invoice_id: str, user_id: str) -> list[str]:
    """確定を取り消す。まとめ入金は同じ入金に紐づく請求書をまとめて取り消す。取り消した請求書番号を返す。"""

    def fn(txn: Txn):
        match = txn.get("matches", invoice_id)
        if not match:
            return []
        tx = txn.get("transactions", match["transaction_id"]) if match.get("transaction_id") else None
        ids = (tx.get("invoice_ids") if tx else None) or [invoice_id]
        invs = [txn.get("invoices", i) for i in ids]
        for i in ids:
            txn.delete("matches", i)
        if tx:
            txn.update("transactions", tx["id"], {"invoice_ids": []})
        nos = [inv["invoice_no"] for inv in invs if inv]
        audit(txn, user_id, "unconfirm", f"請求書 {' + '.join(nos)}")
        return nos

    return store.run_transaction(fn)


# ---------------------------------------------------------------- 取引先・別名

def _alias_doc(client_id: str, alias: str) -> dict:
    return {"client_id": client_id, "alias": alias, "alias_norm": kana.normalize(alias), **_stamp()}


def add_alias(store: Store, client_id: str, alias: str) -> bool:
    return store.create("aliases", alias_id_for(client_id, alias), _alias_doc(client_id, alias))


def delete_alias(store: Store, client_id: str, alias_id: str) -> dict | None:
    doc = store.get("aliases", alias_id)
    if not doc or doc["client_id"] != client_id:
        return None
    store.delete("aliases", alias_id)
    return doc


def set_client_kana(store: Store, client_id: str, name_kana: str) -> None:
    store.update("clients", client_id, {"name_kana": name_kana})


# ---------------------------------------------------------------- 入金明細の一覧・検索

def _matches_query(t: dict, q: str) -> bool:
    if not q:
        return True
    return q in t["payer_kana"] or q in t["description"] or str(t["amount"]) == q.replace(",", "").strip()


def list_transactions(snap: Snapshot, filter: str, q: str, limit: int = 500) -> list[dict]:
    by_id = snap.invoices_by_id
    rows = []
    for t in reversed(snap.transactions):
        matched = bool(t.get("invoice_ids"))
        if (filter == "open" and matched) or (filter == "matched" and not matched) or not _matches_query(t, q):
            continue
        refs = [by_id[i] for i in t.get("invoice_ids", []) if i in by_id]
        rows.append({**t, "invoices": sorted(refs, key=lambda i: i["invoice_no"])})
        if len(rows) >= limit:
            break
    return rows


def search_open_transactions(snap: Snapshot, q: str, limit: int = 50) -> list[dict]:
    return list_transactions(snap, "open", q, limit)
