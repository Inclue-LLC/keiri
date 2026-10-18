from datetime import date

from app import csv_import, matching, repo
from tests.conftest import SAMPLES, add_client, add_invoice, add_tx

TODAY = date(2026, 10, 18)


def _load_samples(store):
    repo.import_invoices(store, csv_import.parse_invoy((SAMPLES / "invoy_invoices.csv").read_bytes()))
    repo.import_transactions(store, csv_import.parse_bank((SAMPLES / "smbc_statement.csv").read_bytes(), "smbc"))
    repo.import_transactions(store, csv_import.parse_bank((SAMPLES / "gmo_aozora_statement.csv").read_bytes(), "gmo"))


def _views(store):
    return {v.invoice["invoice_no"]: v for v in matching.build_views(repo.load(store), today=TODAY)}


def test_statuses_from_samples(store):
    _load_samples(store)
    views = _views(store)

    # 金額一致・日付妥当だが名義が未登録 → 要確認
    v = views["20260901-1"]
    assert v.status == matching.STATUS_REVIEW
    assert v.candidates[0].amount_diff == 0
    assert not v.candidates[0].fee_suspected

    # 660円少ない入金・名義一致 → 手数料差引の可能性ありで要確認
    v = views["20260905-1"]
    assert v.status == matching.STATUS_REVIEW
    assert v.candidates[0].fee_suspected
    assert v.candidates[0].amount_diff == 660
    assert any("手数料差引の可能性あり" in label for label, _ in v.candidates[0].reasons)

    assert views["20260820-1"].status == matching.STATUS_OVERDUE
    assert views["20261001-1"].status == matching.STATUS_UNPAID


def test_alias_raises_name_score(store):
    _load_samples(store)
    before = _views(store)["20260901-1"].candidates[0].score
    repo.add_alias(store, repo.client_id_for("株式会社山田商事"), "ｶ)ﾔﾏﾀﾞｼﾖｳｼﾞ")
    cand = _views(store)["20260901-1"].candidates[0]
    assert cand.score > before
    assert cand.name_score == 1.0


def test_confirmed_match_is_paid_and_tx_not_reused(store):
    _load_samples(store)
    inv = repo.invoice_id_for("20260901-1")
    tx = next(t for t in store.all("transactions") if t["amount"] == 110000)
    repo.confirm(store, [inv], tx["id"], "", "u", False)
    # 同額の別請求書を追加しても、確定済みの入金は候補に出ない
    add_invoice(store, "X-1", repo.client_id_for("株式会社山田商事"), "2026-09-01", "2026-09-30", 110000)
    views = _views(store)
    assert views["20260901-1"].status == matching.STATUS_PAID
    assert views["X-1"].candidates == []
    assert views["X-1"].status == matching.STATUS_OVERDUE


def test_fee_candidate_requires_name_similarity(store):
    """差額ありで名義も合わない入金は日付が合っても候補にしない（ノイズ防止）。"""
    cid = add_client(store, "株式会社サンプル")
    add_invoice(store, "S-1", cid, "2026-10-01", "2026-10-31", 10000)
    tid = add_tx(store, "2026-10-10", 9560, "ベツカイシヤ")
    assert _views(store)["S-1"].candidates == []
    store.update("transactions", tid, {"payer_kana": "カ)サンプル"})
    assert _views(store)["S-1"].candidates[0].amount_diff == 440


def test_fee_over_tolerance_not_candidate(store):
    cid = add_client(store, "X", kana="サンプル")
    add_invoice(store, "S-1", cid, "2026-10-01", "2026-10-31", 10000)
    add_tx(store, "2026-10-10", 9000, "サンプル")
    assert _views(store)["S-1"].candidates == []


def _setup_client(store, invoices, txs):
    cid = add_client(store, "株式会社サンプル", aliases=["カ)サンプル"])
    for no, issue, due, amount in invoices:
        add_invoice(store, no, cid, issue, due, amount)
    for d, amount, payer in txs:
        add_tx(store, d, amount, payer)


def test_combined_payment_candidate(store):
    _setup_client(
        store,
        [("A", "2026-03-09", "2026-04-30", 440000), ("B", "2026-05-02", "2026-05-31", 165000),
         ("C", "2026-05-10", "2026-06-30", 50000)],
        [("2026-05-29", 605000, "カ)サンプル")],
    )
    views = _views(store)
    for no in ("A", "B"):
        c = views[no].candidates[0]
        assert c.is_combined
        assert [g["invoice_no"] for g in c.group] == ["A", "B"]
        assert c.amount_diff == 0
        assert views[no].status == matching.STATUS_REVIEW
    assert views["C"].candidates == []


def test_combined_payment_requires_name(store):
    _setup_client(
        store,
        [("A", "2026-03-09", "2026-04-30", 440000), ("B", "2026-05-02", "2026-05-31", 165000)],
        [("2026-05-29", 605000, "ベツノカイシヤ")],
    )
    assert _views(store)["A"].candidates == []


def test_same_amount_prefers_deposit_near_due(store):
    _setup_client(
        store,
        [("APR", "2026-04-08", "2026-04-30", 165000), ("MAY", "2026-05-02", "2026-05-31", 165000)],
        [("2026-04-30", 165000, "カ)サンプル"), ("2026-05-29", 165000, "カ)サンプル")],
    )
    views = _views(store)
    assert views["APR"].candidates[0].transaction["date"] == "2026-04-30"
    assert views["MAY"].candidates[0].transaction["date"] == "2026-05-29"


def test_fee_candidate_requires_date(store):
    _setup_client(store, [("A", "2026-06-06", "2026-07-31", 34656)], [("2026-03-31", 34289, "カ)サンプル")])
    assert _views(store)["A"].candidates == []
