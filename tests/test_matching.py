from datetime import date

from app import csv_import, db, matching
from app.main import import_invoices, import_transactions
from tests.conftest import SAMPLES

TODAY = date(2026, 10, 18)


def _load_samples(conn):
    import_invoices(conn, csv_import.parse_invoy((SAMPLES / "invoy_invoices.csv").read_bytes()))
    import_transactions(conn, csv_import.parse_bank((SAMPLES / "smbc_statement.csv").read_bytes(), "smbc"))
    import_transactions(conn, csv_import.parse_bank((SAMPLES / "gmo_aozora_statement.csv").read_bytes(), "gmo"))


def _views(conn):
    return {v.invoice["invoice_no"]: v for v in matching.build_views(conn, today=TODAY)}


def test_statuses_from_samples(db_path):
    with db.transaction(db_path) as conn:
        _load_samples(conn)
        views = _views(conn)

    # 金額一致・日付妥当だが名義が未登録 → 要確認
    v = views["INV-2026-0101"]
    assert v.status == matching.STATUS_REVIEW
    assert v.candidates[0].amount_diff == 0
    assert not v.candidates[0].fee_suspected

    # 660円少ない入金・名義一致 → 手数料差引の可能性ありで要確認
    v = views["INV-2026-0102"]
    assert v.status == matching.STATUS_REVIEW
    assert v.candidates[0].fee_suspected
    assert v.candidates[0].amount_diff == 660
    assert any("手数料差引の可能性あり" in label for label, _ in v.candidates[0].reasons)

    assert views["INV-2026-0103"].status == matching.STATUS_OVERDUE
    assert views["INV-2026-0104"].status == matching.STATUS_UNPAID


def test_alias_raises_name_score(db_path):
    with db.transaction(db_path) as conn:
        _load_samples(conn)
        client_id = conn.execute("SELECT id FROM clients WHERE name = '株式会社山田商事'").fetchone()[0]
        before = _views(conn)["INV-2026-0101"].candidates[0].score
        conn.execute(
            "INSERT INTO client_aliases (client_id, alias, alias_norm) VALUES (?, 'ｶ)ﾔﾏﾀﾞｼﾖｳｼﾞ', 'ヤマダシヨウジ')",
            (client_id,),
        )
        cand = _views(conn)["INV-2026-0101"].candidates[0]
    assert cand.score > before
    assert cand.name_score == 1.0


def test_confirmed_match_is_paid_and_tx_not_reused(db_path):
    with db.transaction(db_path) as conn:
        _load_samples(conn)
        conn.execute("INSERT INTO users (username, password_hash) VALUES ('u', 'x')")
        inv = conn.execute("SELECT id FROM invoices WHERE invoice_no = 'INV-2026-0101'").fetchone()[0]
        tx = conn.execute("SELECT id FROM transactions WHERE amount = 110000").fetchone()[0]
        conn.execute("INSERT INTO matches (invoice_id, transaction_id, confirmed_by) VALUES (?, ?, 1)", (inv, tx))
        # 同額の別請求書を追加しても、確定済みの入金は候補に出ない
        conn.execute(
            "INSERT INTO invoices (invoice_no, client_id, issue_date, due_date, amount) "
            "SELECT 'X-1', client_id, issue_date, due_date, amount FROM invoices WHERE id = ?",
            (inv,),
        )
        views = _views(conn)
    assert views["INV-2026-0101"].status == matching.STATUS_PAID
    assert views["X-1"].candidates == []
    assert views["X-1"].status == matching.STATUS_OVERDUE


def test_fee_candidate_requires_name_similarity(db_path):
    """差額ありで名義も合わない入金は日付が合っても候補にしない（ノイズ防止）。"""
    with db.transaction(db_path) as conn:
        conn.execute("INSERT INTO clients (id, name) VALUES (1, '株式会社サンプル')")
        conn.execute(
            "INSERT INTO invoices (invoice_no, client_id, issue_date, due_date, amount) "
            "VALUES ('S-1', 1, '2026-10-01', '2026-10-31', 10000)"
        )
        conn.execute(
            "INSERT INTO transactions (bank, date, amount, description, payer_kana, dedupe_key) "
            "VALUES ('smbc', '2026-10-10', 9560, '振込 ﾍﾞﾂｶｲｼﾔ', 'ベツカイシヤ', 'k1')"
        )
        assert _views(conn)["S-1"].candidates == []
        conn.execute("UPDATE transactions SET payer_kana = 'カ)サンプル'")
        assert _views(conn)["S-1"].candidates[0].amount_diff == 440


def test_fee_over_tolerance_not_candidate(db_path):
    with db.transaction(db_path) as conn:
        conn.execute("INSERT INTO clients (id, name, name_kana) VALUES (1, 'X', 'サンプル')")
        conn.execute(
            "INSERT INTO invoices (invoice_no, client_id, issue_date, due_date, amount) "
            "VALUES ('S-1', 1, '2026-10-01', '2026-10-31', 10000)"
        )
        conn.execute(
            "INSERT INTO transactions (bank, date, amount, description, payer_kana, dedupe_key) "
            "VALUES ('smbc', '2026-10-10', 9000, '', 'サンプル', 'k1')"
        )
        assert _views(conn)["S-1"].candidates == []
