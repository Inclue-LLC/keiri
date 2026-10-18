"""Google アカウント（Google Workspace）でのログイン（OpenID Connect）。"""
from __future__ import annotations

import base64
import json
import time
from urllib.parse import urlencode

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = {"accounts.google.com", "https://accounts.google.com"}


class LoginError(Exception):
    pass


def authorization_url(client_id: str, redirect_uri: str, state: str, nonce: str, domain: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "prompt": "select_account",
    }
    if domain:
        params["hd"] = domain  # アカウント選択画面を会社のアカウントに絞る（検証はサーバー側で別途行う）
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str, client_id: str, client_secret: str, redirect_uri: str) -> dict:
    """認可コードを ID トークンに交換し、そのクレームを返す。"""
    resp = httpx.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=10,
    )
    if resp.status_code != 200:
        raise LoginError("Google からトークンを取得できませんでした")
    id_token = resp.json().get("id_token", "")
    # TLS で Google のトークンエンドポイントから直接受け取った ID トークンは署名検証を省略できる
    # （OpenID Connect Core 1.0 3.1.3.7）。発行者・宛先・有効期限・nonce は validate_claims で確認する
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as e:
        raise LoginError("ID トークンを読み取れませんでした") from e


def validate_claims(
    claims: dict, client_id: str, nonce: str, domain: str, allowed_emails: set[str], now: float | None = None
) -> str:
    """ID トークンのクレームを検証し、ログインを許可するメールアドレスを返す。"""
    now = time.time() if now is None else now
    if claims.get("iss") not in ISSUERS or claims.get("aud") != client_id:
        raise LoginError("ID トークンの発行元が不正です")
    if float(claims.get("exp", 0)) < now:
        raise LoginError("ログインの有効期限が切れました。もう一度お試しください")
    if not nonce or claims.get("nonce") != nonce:
        raise LoginError("ログイン要求が一致しません。もう一度お試しください")
    email = str(claims.get("email", "")).lower()
    if not email or not claims.get("email_verified"):
        raise LoginError("メールアドレスが確認できません")
    if domain and (claims.get("hd") != domain or not email.endswith("@" + domain)):
        raise LoginError(f"{domain} のアカウントでログインしてください")
    if allowed_emails and email not in allowed_emails:
        raise LoginError("このアカウントには利用が許可されていません")
    return email
