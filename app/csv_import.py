"""INVOY 請求書 CSV と銀行入出金明細 CSV のパーサー。

各サービスの CSV は列名や前置き行が微妙に異なるため、列は「候補名リスト」で探す。
列名が合わない場合は *_COLUMNS の候補に追加すれば対応できる。
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from . import kana


class CsvFormatError(ValueError):
    pass


@dataclass
class ParsedInvoice:
    invoice_no: str
    client_name: str
    client_kana: str
    subject: str
    issue_date: str | None
    due_date: str | None
    amount: int
    memo: str = ""


@dataclass
class ParsedTransaction:
    bank: str
    date: str
    amount: int
    description: str
    payer_kana: str
    balance: int | None
    dedupe_key: str


@dataclass
class ParseResult:
    rows: list = field(default_factory=list)
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)


BANKS = {
    "smbc": "三井住友銀行",
    "gmo": "GMOあおぞらネット銀行",
}

# 先に書いたものほど優先。完全一致 → 部分一致の順に探す
INVOICE_COLUMNS = {
    "invoice_no": ["請求書番号", "請求番号", "書類番号", "請求書No", "請求書NO", "No"],
    "client_name": ["請求先", "請求先名", "取引先名", "取引先", "宛先", "宛名", "顧客名", "会社名"],
    "client_kana": ["取引先名カナ", "取引先カナ", "フリガナ", "カナ"],
    "subject": ["件名", "タイトル", "案件名"],
    "issue_date": ["請求日", "発行日", "請求書日付", "日付"],
    "due_date": ["お支払い期限", "お支払期限", "支払い期限", "支払期限", "支払期日", "入金期日", "入金予定日", "振込期限"],
    "amount": [
        "請求額", "差引請求額", "差引請求金額", "ご請求金額", "請求金額", "合計金額(税込)", "税込合計",
        "税込合計金額", "税込金額", "合計金額", "合計", "金額",
    ],
    "status": ["ステータス", "状態"],
    "memo": ["備考", "メモ"],
}

BANK_COLUMNS = {
    "date": ["年月日", "日付", "取引日", "お取引日", "お取扱日", "勘定日", "取扱日付"],
    "deposit": ["お預入れ", "お預入れ金額", "入金金額", "入金額", "お預り金額", "預入金額", "入金"],
    "withdrawal": ["お引出し", "お引出し金額", "出金金額", "出金額", "お支払金額", "出金"],
    "description": ["お取り扱い内容", "お取扱内容", "摘要", "取引内容", "お取引内容", "内容", "振込依頼人名"],
    "balance": ["残高", "差引残高"],
}


def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp932"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise CsvFormatError("文字コードを判別できません（UTF-8 / Shift_JIS に対応）")


def _norm_header(h: str) -> str:
    return re.sub(r"[\s\"'　]", "", unicodedata.normalize("NFKC", h or ""))


# 部分一致で拾うと誤る列名
_EXCLUDE_PARTIAL = ("税抜", "消費税", "小計", "源泉")


def _find_columns(header: list[str], spec: dict[str, list[str]]) -> dict[str, int]:
    normed = [_norm_header(h) for h in header]
    found: dict[str, int] = {}
    used: set[int] = set()
    for key, candidates in spec.items():
        cands = [_norm_header(c) for c in candidates]
        idx = next((normed.index(c) for c in cands if c in normed and normed.index(c) not in used), None)
        if idx is None:
            idx = next(
                (
                    i for c in cands for i, h in enumerate(normed)
                    if c in h and i not in used and not any(x in h for x in _EXCLUDE_PARTIAL)
                ),
                None,
            )
        if idx is not None:
            found[key] = idx
            used.add(idx)
    return found


def _locate_header(rows: list[list[str]], spec: dict, required: tuple[str, ...]) -> tuple[int, dict[str, int]]:
    """前置き行がある CSV に備え、先頭 20 行から必須列が揃う行をヘッダーとみなす。"""
    for i, row in enumerate(rows[:20]):
        cols = _find_columns(row, spec)
        if all(k in cols for k in required):
            return i, cols
    labels = "・".join(spec[k][0] for k in required)
    raise CsvFormatError(f"ヘッダー行が見つかりません（必要な列: {labels}）")


_WAREKI = {"R": 2018, "令和": 2018, "H": 1988, "平成": 1988}


def parse_date(value: str) -> str | None:
    """各種日付表記を ISO 形式（YYYY-MM-DD）に変換する。"""
    s = unicodedata.normalize("NFKC", value or "").strip()
    if not s:
        return None
    m = re.match(r"^(R|H|令和|平成)\s*(\d{1,2}|元)[./年-](\d{1,2})[./月-](\d{1,2})日?", s)
    if m:
        y = 1 if m.group(2) == "元" else int(m.group(2))
        return date(_WAREKI[m.group(1)] + y, int(m.group(3)), int(m.group(4))).isoformat()
    m = re.match(r"^(\d{4})[./年-](\d{1,2})[./月-](\d{1,2})日?", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    raise ValueError(f"日付を解釈できません: {value}")


def issue_date_from_invoice_no(invoice_no: str) -> str | None:
    """請求日の列がない INVOY CSV 向けに、請求書番号の先頭8桁（例: 20260105-1）を請求日とみなす。"""
    m = re.match(r"^(\d{4})(\d{2})(\d{2})(?:\D|$)", invoice_no)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def parse_amount(value: str) -> int | None:
    s = unicodedata.normalize("NFKC", value or "")
    s = re.sub(r"[,\s円¥\\]", "", s)
    if s in ("", "-"):
        return None
    neg = s.startswith("-") or s.startswith("△") or s.startswith("▲")
    s = s.lstrip("-△▲")
    num = int(round(float(s)))
    return -num if neg else num


def _cell(row: list[str], cols: dict[str, int], key: str) -> str:
    idx = cols.get(key)
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def _read_rows(data: bytes) -> list[list[str]]:
    text = decode(data)
    return list(csv.reader(io.StringIO(text)))


def parse_invoy(data: bytes) -> ParseResult:
    rows = _read_rows(data)
    header_idx, cols = _locate_header(rows, INVOICE_COLUMNS, ("invoice_no", "client_name", "amount"))
    result = ParseResult()
    seen: dict[str, ParsedInvoice] = {}
    for lineno, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        if not any(c.strip() for c in row):
            continue
        invoice_no = _cell(row, cols, "invoice_no")
        if not invoice_no:
            result.skipped += 1
            continue
        # INVOY のステータスは「下書き」のまま運用されていることが多いため、削除済み以外は取り込む
        if "削除" in _cell(row, cols, "status"):
            result.skipped += 1
            continue
        try:
            amount = parse_amount(_cell(row, cols, "amount"))
            if amount is None:
                raise ValueError("金額が空です")
            inv = ParsedInvoice(
                invoice_no=invoice_no,
                client_name=_cell(row, cols, "client_name"),
                client_kana=_cell(row, cols, "client_kana"),
                subject=_cell(row, cols, "subject"),
                issue_date=parse_date(_cell(row, cols, "issue_date")) or issue_date_from_invoice_no(invoice_no),
                due_date=parse_date(_cell(row, cols, "due_date")),
                amount=amount,
                memo=_cell(row, cols, "memo"),
            )
        except ValueError as e:
            result.skipped += 1
            result.warnings.append(f"{lineno}行目: {e}")
            continue
        if invoice_no in seen:
            # 明細行ごとに出力された CSV では同じ請求書番号が繰り返される
            if seen[invoice_no].amount != inv.amount:
                result.warnings.append(f"{lineno}行目: 請求書番号 {invoice_no} の金額が行ごとに異なります（先頭行を採用）")
            continue
        seen[invoice_no] = inv
        result.rows.append(inv)
    return result


def parse_bank(data: bytes, bank: str) -> ParseResult:
    if bank not in BANKS:
        raise CsvFormatError(f"未対応の銀行です: {bank}")
    rows = _read_rows(data)
    header_idx, cols = _locate_header(rows, BANK_COLUMNS, ("date", "deposit", "description"))
    result = ParseResult()
    occurrences: dict[str, int] = {}
    for lineno, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        if not any(c.strip() for c in row):
            continue
        try:
            d = parse_date(_cell(row, cols, "date"))
            deposit = parse_amount(_cell(row, cols, "deposit"))
            balance = parse_amount(_cell(row, cols, "balance"))
        except ValueError as e:
            result.skipped += 1
            result.warnings.append(f"{lineno}行目: {e}")
            continue
        if not d or not deposit or deposit <= 0:
            # 出金行・合計行などは対象外
            result.skipped += 1
            continue
        description = unicodedata.normalize("NFKC", _cell(row, cols, "description"))
        base = f"{bank}|{d}|{deposit}|{description}|{balance}"
        # 同日・同額・同名義の入金が複数あっても区別できるようファイル内の出現順を含める
        occurrences[base] = occurrences.get(base, 0) + 1
        key = hashlib.sha256(f"{base}|{occurrences[base]}".encode()).hexdigest()
        result.rows.append(ParsedTransaction(
            bank=bank,
            date=d,
            amount=deposit,
            description=description,
            payer_kana=kana.extract_payer(description),
            balance=balance,
            dedupe_key=key,
        ))
    return result
