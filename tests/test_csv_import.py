import pytest

from app import csv_import
from tests.conftest import SAMPLES


def test_parse_invoy_sample():
    r = csv_import.parse_invoy((SAMPLES / "invoy_invoices.csv").read_bytes())
    assert [i.invoice_no for i in r.rows] == ["20260901-1", "20260905-1", "20260820-1", "20261001-1"]
    assert r.skipped == 0  # ステータスが「下書き」でも取り込む
    first = r.rows[0]
    assert first.client_name == "株式会社山田商事"
    assert first.amount == 110000
    assert first.issue_date == "2026-09-01"  # 請求日の列がないので請求書番号から
    assert first.due_date == "2026-09-30"
    assert r.rows[3].subject == "10月分 コンサルティング（1, 2回目）"
    assert r.rows[3].memo == "11月分と合算して入金予定"


@pytest.mark.parametrize(
    "invoice_no, expected",
    [("20260105-1", "2026-01-05"), ("20260105", "2026-01-05"), ("20261340-1", None), ("INV-001", None)],
)
def test_issue_date_from_invoice_no(invoice_no, expected):
    assert csv_import.issue_date_from_invoice_no(invoice_no) == expected


def test_parse_invoy_line_items_dedup():
    data = (
        "請求書番号,取引先名,請求日,お支払期限,品目,請求金額\n"
        "A-1,株式会社X,2026/01/10,2026/02/28,作業A,\"33,000\"\n"
        "A-1,株式会社X,2026/01/10,2026/02/28,作業B,\"33,000\"\n"
    ).encode("utf-8")
    r = csv_import.parse_invoy(data)
    assert len(r.rows) == 1
    assert r.rows[0].amount == 33000
    assert r.rows[0].due_date == "2026-02-28"


def test_parse_smbc_sample_only_deposits():
    r = csv_import.parse_bank((SAMPLES / "smbc_statement.csv").read_bytes(), "smbc")
    assert [(t.date, t.amount) for t in r.rows] == [("2026-09-30", 110000), ("2026-10-05", 77000)]
    assert r.rows[0].payer_kana == "カ)ヤマダシヨウジ"
    assert r.skipped == 1  # 出金


def test_parse_gmo_sample():
    r = csv_import.parse_bank((SAMPLES / "gmo_aozora_statement.csv").read_bytes(), "gmo")
    assert len(r.rows) == 1
    t = r.rows[0]
    assert (t.date, t.amount, t.payer_kana, t.balance) == ("2026-10-10", 54340, "ABC(ド", 820340)


def test_bank_preamble_rows_and_wareki():
    data = (
        "入出金明細\n"
        "口座番号,1234567\n"
        "\n"
        "年月日,お引出し,お預入れ,お取り扱い内容,残高\n"
        "R08.04.01,,\"1,000\",振込 ﾔﾏﾀﾞ,\"5,000\"\n"
    ).encode("cp932")
    r = csv_import.parse_bank(data, "smbc")
    assert r.rows[0].date == "2026-04-01"
    assert r.rows[0].amount == 1000


def test_same_day_same_amount_deposits_are_distinct():
    data = (
        "日付,摘要,入金金額,出金金額,残高\n"
        "2026/10/01,振込 ﾔﾏﾀﾞ,1000,,\n"
        "2026/10/01,振込 ﾔﾏﾀﾞ,1000,,\n"
    ).encode()
    r = csv_import.parse_bank(data, "gmo")
    assert len({t.dedupe_key for t in r.rows}) == 2


def test_missing_header_raises():
    with pytest.raises(csv_import.CsvFormatError):
        csv_import.parse_bank("a,b,c\n1,2,3\n".encode(), "smbc")


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026/1/5", "2026-01-05"),
        ("2026-01-05", "2026-01-05"),
        ("2026年1月5日", "2026-01-05"),
        ("20260105", "2026-01-05"),
        ("R8.1.5", "2026-01-05"),
        ("令和8年1月5日", "2026-01-05"),
        ("H31.04.30", "2019-04-30"),
        ("", None),
    ],
)
def test_parse_date(value, expected):
    assert csv_import.parse_date(value) == expected


def test_parse_amount():
    assert csv_import.parse_amount("¥1,234円") == 1234
    assert csv_import.parse_amount("１，０００") == 1000
    assert csv_import.parse_amount("") is None
    assert csv_import.parse_amount("-500") == -500


ZENGIN = (
    '"データ区分","種別コード","コード区分","作成日","勘定日(自)","勘定日(至)","銀行コード","銀行名（ｶﾅ）","支店コード",'
    '"支店名（ｶﾅ）","ダミー","預金種目","口座番号","口座名","貸越区分","通帳・証書区分","取引前残高","ダミー"\n'
    '1,03,0,081018,080101,080131,0009,"ﾐﾂｲｽﾐﾄﾓ",123,"ｻﾝﾌﾟﾙ",000,1,0001234567,"ｶ) ｻﾝﾌﾟﾙ",1,1,100," "\n'
    '"データ区分","照会番号","勘定日","預入・払出日","入払区分","取引区分","取引金額","うち他店券金額","交換呈示日",'
    '"不渡返還日","手形・小切手区分","手形・小切手番号","僚店番号","振込依頼人コード","振込依頼人名または契約者番号",'
    '"仕向銀行名","仕向支店名","摘要内容","EDI情報","ダミー"\n'
    '{rows}'
    '"データ区分","入金件数","入金額合計","出金件数","出金額合計","貸越区分","取引後残高","データレコード件数","ダミー"\n'
    '8,0,0,0,0,1,0,0," "\n'
    '"データ区分","レコード総件数","口座数","ダミー"\n'
    '9,0,1," "\n'
)


def _zengin(*rows: str) -> bytes:
    return ZENGIN.format(rows="".join(r + "\n" for r in rows)).encode("cp932")


def test_parse_zengin_rows():
    data = _zengin(
        '2,05000001,080105,080105,1,11,000000009996,000000000000,,,,,,,"ｶ)ﾌｱﾝﾎﾞﾂｸｽ 0001","ﾐｽﾞﾎ","ｸﾀﾞﾝ","ﾌﾘｺﾐ",""," "',
        '2,05000002,080105,080105,2,14,000000173820,000000000000,,,,,,,"6495","","","ｺｳｻﾞﾌﾘｶｴ","0",',
        '2,13000004,080113,080113,1,14,000002240000,000000000000,,,,,,,"","","","ｶﾞｲｺｸ ｶﾝｹｲ ﾋｼﾑｹｿｳｷﾝ4",""," "',
    )
    r = csv_import.parse_bank(data, "smbc")
    assert r.skipped == 1  # 出金
    first, second = r.rows
    assert (first.date, first.amount, first.payer_kana) == ("2026-01-05", 9996, "カ)フアンボツクス 0001")
    assert first.description == "フリコミ カ)フアンボツクス 0001 (ミズホ クダン)"
    # 振込依頼人名がない入金（外国送金など）は摘要を名義として扱う
    assert (second.date, second.amount, second.payer_kana) == ("2026-01-13", 2240000, "ガイコク カンケイ ヒシムケソウキン4")


def test_zengin_dedupe_across_files():
    row = '2,30000001,080130,080130,1,11,000000060805,000000000000,,,,,,,"ｶ)ﾃｽﾄ","ﾐﾂｲｽﾐﾄﾓ","ﾋﾋﾞﾔ","ﾌﾘｺﾐ",""," "'
    other = '2,30000002,080130,080130,1,11,000000060805,000000000000,,,,,,,"ｶ)ﾃｽﾄ","ﾐﾂｲｽﾐﾄﾓ","ﾋﾋﾞﾔ","ﾌﾘｺﾐ",""," "'
    a = csv_import.parse_bank(_zengin(row), "smbc").rows
    b = csv_import.parse_bank(_zengin(other, row), "smbc").rows
    # 期間が重なる別ファイルでも同じ明細は同じキー、同日同額の別明細は別キー
    assert a[0].dedupe_key == b[1].dedupe_key
    assert b[0].dedupe_key != b[1].dedupe_key


def test_parse_zengin_date():
    assert csv_import.parse_zengin_date("081018") == "2026-10-18"
    with pytest.raises(ValueError):
        csv_import.parse_zengin_date("2026-10-18")
