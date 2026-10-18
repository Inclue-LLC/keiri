"""振込名義（カナ）の正規化と類似度判定。

銀行明細の振込名義は「ｶ)ﾔﾏﾀﾞｼﾖｳｼﾞ」「ﾔﾏﾀﾞｼｮｳｼﾞ(ｶ」のように
半角カナ・法人略語・拗音の大文字化などの揺れがあるため、比較前に正規化する。
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

_SMALL_TO_LARGE = str.maketrans(
    "ァィゥェォッャュョヮヵヶ",
    "アイウエオツヤユヨワカケ",
)

# 法人格の略語（銀行の振込名義で使われるもの）
_ENTITY_ABBREVIATIONS = [
    "カ", "ユ", "ド", "メ", "シ", "ゴ", "ザイ", "シヤ", "ガク", "イ", "セイ", "トク",
    "ギヨ", "ノウ", "ソ", "フク", "ベン", "ゼイ", "シホ", "ギ", "エイ", "ケン", "キヨウ",
    "シユウ", "ドクヨウ", "ゴウ",
]
_ENTITY_WORDS = [
    "カブシキガイシヤ", "ユウゲンガイシヤ", "ゴウドウガイシヤ", "ゴウメイガイシヤ", "ゴウシガイシヤ",
    "イツパンシヤダンホウジン", "イツパンザイダンホウジン", "トクテイヒエイリカツドウホウジン",
    "イリヨウホウジン", "シヤカイフクシホウジン", "ガツコウホウジン", "ゼイリシホウジン",
]

# 請求書側の取引先名（漢字表記）に含まれる法人格
_ENTITY_WORDS += [
    "株式会社", "有限会社", "合同会社", "合名会社", "合資会社", "一般社団法人", "一般財団法人",
    "特定非営利活動法人", "NPO法人", "医療法人", "社会福祉法人", "学校法人", "税理士法人", "(株)", "(有)", "(同)",
]

_abbr = "|".join(sorted(map(re.escape, _ENTITY_ABBREVIATIONS), key=len, reverse=True))
# 「(カ)」「カ)」「(カ」を先頭・末尾・中間から除去
_ENTITY_RE = re.compile(rf"\((?:{_abbr})\)|^(?:{_abbr})\)|\((?:{_abbr})$|(?<=[\s])(?:{_abbr})\)")
_WORDS_RE = re.compile("|".join(map(re.escape, _ENTITY_WORDS)))

# 摘要の先頭に付く取引種別（NFKC 正規化後の表記で列挙）
_PREFIXES = [
    "振込入金", "振込", "振替", "IB振込", "テレ振込", "ネット振込", "電信振込",
    "フリコミニュウキン", "フリコミニユウキン", "フリコミ", "テレフリコミ", "ネットフリコミ", "デンシンフリコミ",
]
_PREFIX_RE = re.compile(
    "^(?:" + "|".join(map(re.escape, sorted(_PREFIXES, key=len, reverse=True))) + r")[\s:]*"
)


def extract_payer(description: str) -> str:
    """銀行摘要から振込名義部分を取り出す（取引種別の接頭辞を外す）。"""
    s = unicodedata.normalize("NFKC", description or "").strip()
    prev = None
    while prev != s:
        prev = s
        s = _PREFIX_RE.sub("", s).strip()
    return s


def normalize(name: str) -> str:
    """比較用に正規化した文字列を返す。"""
    s = unicodedata.normalize("NFKC", name or "")
    # ひらがな → カタカナ
    s = "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)
    s = s.upper().translate(_SMALL_TO_LARGE)
    s = re.sub(r"[‐‑‒–—―−ｰ-]", "ー", s)
    # 振込依頼人コード（先頭の数字列）を除去
    s = re.sub(r"^\d{6,}\s*", "", s)
    s = _ENTITY_RE.sub(" ", s.strip())
    s = _WORDS_RE.sub("", s)
    s = re.sub(r"[\s・.,，．、()（）「」\"'/]", "", s)
    s = s.replace("ー", "")
    return s


def similarity(a: str, b: str) -> float:
    """正規化済み文字列同士の類似度（0〜1）。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    shorter, longer = sorted((a, b), key=len)
    if len(shorter) >= 3 and shorter in longer:
        return 0.9
    return SequenceMatcher(None, a, b).ratio()
