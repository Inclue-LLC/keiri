import pytest

from app import kana


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("ｶ)ﾔﾏﾀﾞｼｮｳｼﾞ", "ヤマダシヨウジ"),
        ("ﾔﾏﾀﾞｼﾖｳｼﾞ(ｶ", "ヤマダシヨウジ"),
        ("(ｶ)ﾔﾏﾀﾞｼﾖｳｼﾞ", "ヤマダシヨウジ"),
        ("カブシキガイシャ　ヤマダショウジ", "ヤマダシヨウジ"),
        ("株式会社山田商事", "山田商事"),
        ("合同会社ABC", "ABC"),
        ("ABC(ﾄﾞ", "ABC"),
        ("1234567890 ﾀﾅｶ ﾀﾛｳ", "タナカタロウ"),
        ("やまだ", "ヤマダ"),
        ("ｻｰﾋﾞｽ", "サビス"),
        ("ｶ)ﾌｱﾝﾎﾞﾂｸｽ 0001", "フアンボツクス"),
        ("ｶ)ﾏﾙｲ(0101", "マルイ"),
        ("3 ﾆﾂﾎﾟﾝﾕｳﾋﾞﾝ (ｶ", "ニツポンユウビン"),
        ("ﾄﾞ)ﾌﾞﾘﾂﾁﾞ ｻﾞ ｽﾀｲﾙ", "ブリツヂザスタイル"),
    ],
)
def test_normalize(raw, expected):
    assert kana.normalize(raw) == expected


@pytest.mark.parametrize(
    "description, expected",
    [
        ("振込　ｶ)ﾔﾏﾀﾞｼﾖｳｼﾞ", "カ)ヤマダシヨウジ"),
        ("ﾌﾘｺﾐ ﾀﾅｶ ﾀﾛｳ", "タナカ タロウ"),
        ("振込入金 ｽｽﾞｷ", "スズキ"),
        ("振込  カ）コモジユ", "カ)コモジユ"),
        ("振込  ＳＴＵＤＩＯ　ＨＡＮＥＵＬ　ストライプジヤパン（カ", "STUDIO HANEUL ストライプジヤパン(カ"),
        ("ﾃﾞﾝｷﾘﾖｳｷﾝ", "デンキリヨウキン"),
    ],
)
def test_extract_payer(description, expected):
    assert kana.extract_payer(description) == expected


def test_similarity():
    assert kana.similarity("ヤマダシヨウジ", "ヤマダシヨウジ") == 1.0
    assert kana.similarity("ヤマダシヨウジトウキヨウシテン", "ヤマダシヨウジ") == 0.9
    assert kana.similarity("ヤマダ", "スズキ") < 0.5
    assert kana.similarity("", "ヤマダ") == 0.0
