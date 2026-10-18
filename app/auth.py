"""パスワードハッシュ、ログイン試行制限、CSRF トークン。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict

_N, _R, _P = 2**14, 8, 1

# ログイン失敗の上限（15分間）。ユーザー名ごとと、接続元 IP ごとに数える
MAX_FAILURES = 5
MAX_FAILURES_PER_IP = 20
LOCK_SECONDS = 15 * 60
_failures: dict[str, list[float]] = defaultdict(list)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    return "scrypt${}${}${}${}${}".format(
        _N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    actual = hashlib.scrypt(
        password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p)
    )
    return hmac.compare_digest(actual, base64.b64decode(digest))


def validate_new_password(password: str) -> str | None:
    if len(password) < 10:
        return "パスワードは10文字以上にしてください"
    return None


def _keys(username: str, ip: str) -> list[tuple[str, int]]:
    return [(f"user:{username}", MAX_FAILURES), (f"ip:{ip}", MAX_FAILURES_PER_IP)]


def is_locked(username: str, ip: str = "") -> bool:
    now = time.time()
    for key, limit in _keys(username, ip):
        _failures[key] = [t for t in _failures[key] if now - t < LOCK_SECONDS]
        if len(_failures[key]) >= limit:
            return True
    return False


def record_failure(username: str, ip: str = "") -> None:
    for key, _ in _keys(username, ip):
        _failures[key].append(time.time())


def clear_failures(username: str) -> None:
    _failures.pop(f"user:{username}", None)


def csrf_token(session: dict) -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def check_csrf(session: dict, token: str | None) -> bool:
    expected = session.get("csrf")
    return bool(expected and token and hmac.compare_digest(expected, token))
