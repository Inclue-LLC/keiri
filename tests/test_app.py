import pytest
from fastapi.testclient import TestClient

from app import auth, config, google_auth, repo
from app.main import create_app
from app.store import FileStore
from tests.conftest import SAMPLES, add_client, add_invoice, add_tx, csrf_of


def test_requires_login(client):
    for path in ["/", "/upload", "/transactions", "/clients", "/users"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].startswith("/login")


def test_login_failure_and_lockout(client):
    token = csrf_of(client)
    for _ in range(5):
        r = client.post("/login", data={"username": "keiri", "password": "wrong", "csrf": token})
        assert r.status_code == 401
    r = client.post("/login", data={"username": "keiri", "password": "correct-horse-battery", "csrf": token})
    assert r.status_code == 401
    assert "上限" in r.text


def test_login_rejects_missing_csrf(client):
    client.get("/login")
    r = client.post("/login", data={"username": "keiri", "password": "correct-horse-battery"})
    assert r.status_code == 400


def test_open_redirect_blocked(client):
    token = csrf_of(client)
    r = client.post(
        "/login",
        data={"username": "keiri", "password": "correct-horse-battery", "csrf": token, "next": "//evil.example"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"


def _upload(client, kind, filename):
    token = csrf_of(client, "/upload")
    return client.post(
        "/upload",
        data={"kind": kind, "csrf": token},
        files={"file": (filename, (SAMPLES / filename).read_bytes(), "text/csv")},
    )


def test_full_flow(logged_in, store):
    c = logged_in
    assert "新規4件" in _upload(c, "invoy", "invoy_invoices.csv").text
    assert "新規2件" in _upload(c, "smbc", "smbc_statement.csv").text
    assert "新規2件" in _upload(c, "gmo", "gmo_aozora_statement.csv").text
    # 同じ明細の再取込は重複しない
    assert "新規0件" in _upload(c, "smbc", "smbc_statement.csv").text
    assert "invoy_invoices.csv" in c.get("/upload").text  # 取込履歴

    page = c.get("/").text
    assert "要確認" in page and "期日超過" in page and "手数料差引の可能性あり" in page

    inv = store.get("invoices", repo.invoice_id_for("20260901-1"))
    tx = next(t for t in store.all("transactions") if t["amount"] == 110000)
    assert "この入金で確定" in c.get(f"/invoices/{inv['id']}").text
    assert "11月分と合算して入金予定" in c.get(f"/invoices/{repo.invoice_id_for('20261001-1')}").text

    token = csrf_of(c, f"/invoices/{inv['id']}")
    r = c.post(
        f"/invoices/{inv['id']}/confirm",
        data={"transaction_id": tx["id"], "register_alias": "1", "note": "確認済", "csrf": token},
    )
    assert "入金済として確定しました" in r.text
    assert "確定済" in r.text and "確定者: keiri" in r.text

    aliases = [a for a in store.all("aliases") if a["client_id"] == inv["client_id"]]
    assert [a["alias"] for a in aliases] == ["カ)ヤマダシヨウジ"]
    assert store.get("matches", inv["id"])["confirmed_by"] == repo.user_id_for("keiri")
    assert store.get("transactions", tx["id"])["invoice_ids"] == [inv["id"]]

    # 消込済の入金を別の請求書に使うことはできない
    other = repo.invoice_id_for("20261001-1")
    r = c.post(f"/invoices/{other}/confirm", data={"transaction_id": tx["id"], "csrf": token})
    assert "既に消込済" in r.text

    # 取消
    r = c.post(f"/invoices/{inv['id']}/unconfirm", data={"csrf": token})
    assert "確定を取り消しました" in r.text
    assert store.all("matches") == []
    assert store.get("transactions", tx["id"])["invoice_ids"] == []

    assert "confirm" in c.get("/users").text  # 操作履歴


def test_manual_confirm_requires_note(logged_in):
    _upload(logged_in, "invoy", "invoy_invoices.csv")
    inv = repo.invoice_id_for("20260820-1")
    token = csrf_of(logged_in, f"/invoices/{inv}")
    r = logged_in.post(f"/invoices/{inv}/confirm-manual", data={"note": " ", "csrf": token})
    assert "理由" in r.text
    r = logged_in.post(f"/invoices/{inv}/confirm-manual", data={"note": "現金受領", "csrf": token})
    assert "明細なし" in r.text
    r = logged_in.post(f"/invoices/{inv}/confirm-manual", data={"note": "二重", "csrf": token})
    assert "既に消込済" in r.text


def test_alias_crud(logged_in, store):
    _upload(logged_in, "invoy", "invoy_invoices.csv")
    cid = repo.client_id_for("合同会社ABC")
    token = csrf_of(logged_in, f"/clients/{cid}")
    r = logged_in.post(f"/clients/{cid}/aliases", data={"alias": "ｴｰﾋﾞｰｼｰﾎｰﾙﾃﾞｨﾝｸﾞｽ", "csrf": token})
    assert "登録しました" in r.text
    r = logged_in.post(f"/clients/{cid}/aliases", data={"alias": "エービーシーホールディングス", "csrf": token})
    assert "既に登録" in r.text
    assert "ｴｰﾋﾞｰｼｰﾎｰﾙﾃﾞｨﾝｸﾞｽ" in logged_in.get("/clients").text
    aid = store.all("aliases")[0]["id"]
    r = logged_in.post(f"/clients/{cid}/aliases/{aid}/delete", data={"csrf": token})
    assert "削除しました" in r.text
    assert store.all("aliases") == []

    r = logged_in.post(f"/clients/{cid}/kana", data={"name_kana": "エービーシー", "csrf": token})
    assert "カナ名を更新しました" in r.text
    assert store.get("clients", cid)["name_kana"] == "エービーシー"


def test_upload_rejects_bad_csv(logged_in):
    token = csrf_of(logged_in, "/upload")
    r = logged_in.post(
        "/upload",
        data={"kind": "smbc", "csrf": token},
        files={"file": ("x.csv", b"foo,bar\n1,2\n", "text/csv")},
    )
    assert "ヘッダー行が見つかりません" in r.text


def test_user_management(logged_in):
    token = csrf_of(logged_in, "/users")
    r = logged_in.post("/users", data={"username": "tanaka", "password": "short", "csrf": token})
    assert "10文字以上" in r.text
    r = logged_in.post("/users", data={"username": "tanaka", "password": "long-enough-pw", "csrf": token})
    assert "作成しました" in r.text
    r = logged_in.post("/users", data={"username": "tanaka", "password": "long-enough-pw", "csrf": token})
    assert "既に使われています" in r.text
    r = logged_in.post(
        "/account/password",
        data={"current_password": "correct-horse-battery", "new_password": "another-long-pw", "csrf": token},
    )
    assert "変更しました" in r.text


def test_combined_confirm_and_unconfirm(logged_in, store):
    cid = add_client(store, "株式会社サンプル")
    a = add_invoice(store, "A", cid, "2026-04-01", "2026-05-31", 440000)
    b = add_invoice(store, "B", cid, "2026-04-01", "2026-05-31", 165000)
    tid = add_tx(store, "2026-05-29", 604560, "カ)サンプル")
    page = logged_in.get(f"/invoices/{a}").text
    assert "まとめ入金の可能性" in page and "2件まとめて確定" in page

    token = csrf_of(logged_in, f"/invoices/{a}")
    r = logged_in.post(
        f"/invoices/{a}/confirm",
        data={"transaction_id": tid, "invoice_ids": [a, b], "register_alias": "1", "csrf": token},
    )
    assert "確定しました（2件）" in r.text
    assert store.get("matches", a)["fee_diff"] == 440
    assert store.get("matches", b)["fee_diff"] == 0
    assert "まとめ入金" in logged_in.get(f"/invoices/{b}").text
    tx_page = logged_in.get("/transactions?filter=matched").text
    assert ">A</a> + <a" in tx_page

    r = logged_in.post(f"/invoices/{b}/unconfirm", data={"csrf": token})
    assert "確定を取り消しました（A, B）" in r.text
    assert store.all("matches") == []


def test_confirm_twice_is_rejected(store):
    cid = add_client(store, "株式会社サンプル")
    a = add_invoice(store, "A", cid, "2026-04-01", "2026-05-31", 1000)
    b = add_invoice(store, "B", cid, "2026-04-01", "2026-05-31", 1000)
    tid = add_tx(store, "2026-05-29", 1000, "カ)サンプル")
    repo.confirm(store, [a], tid, "", "u", False)
    with pytest.raises(repo.ConfirmError):
        repo.confirm(store, [b], tid, "", "u", False)
    with pytest.raises(repo.ConfirmError):
        repo.confirm(store, [a], add_tx(store, "2026-05-30", 1000, "x"), "", "u", False)
    assert set(m["id"] for m in store.all("matches")) == {a}


def test_upload_multiple_files(logged_in):
    token = csrf_of(logged_in, "/upload")
    smbc = (SAMPLES / "smbc_statement.csv").read_bytes()
    r = logged_in.post(
        "/upload",
        data={"kind": "smbc", "csrf": token},
        files=[("file", ("a.csv", smbc, "text/csv")), ("file", ("b.csv", smbc, "text/csv"))],
    )
    assert "a.csv: 三井住友銀行 入金 新規2件" in r.text
    assert "b.csv: 三井住友銀行 入金 新規0件・取込済のためスキップ2件" in r.text


def test_reimport_keeps_confirmed_invoice(logged_in, store):
    _upload(logged_in, "invoy", "invoy_invoices.csv")
    inv = repo.invoice_id_for("20260820-1")
    repo.confirm_manual(store, inv, "現金", "u")
    store.update("invoices", inv, {"amount": 1})
    r = _upload(logged_in, "invoy", "invoy_invoices.csv")
    assert "新規0件・更新3件・確定済のためスキップ1件" in r.text
    assert store.get("invoices", inv)["amount"] == 1


def test_initial_user_from_env(monkeypatch):
    monkeypatch.setattr(config, "INITIAL_USER", "admin")
    monkeypatch.setattr(config, "INITIAL_PASSWORD", "initial-password")
    store = FileStore()
    create_app(store=store, secret_key="s")
    create_app(store=store, secret_key="s")  # 2回目の起動では作り直さない
    assert [u["username"] for u in repo.list_users(store)] == ["admin"]

    monkeypatch.setattr(config, "INITIAL_USER", "other")
    create_app(store=store, secret_key="s")
    assert len(repo.list_users(store)) == 1


def test_initial_user_rejects_short_password(monkeypatch):
    monkeypatch.setattr(config, "INITIAL_USER", "admin")
    monkeypatch.setattr(config, "INITIAL_PASSWORD", "short")
    with pytest.raises(RuntimeError):
        create_app(store=FileStore(), secret_key="s")


def test_session_secret_is_shared(store):
    assert repo.session_secret(store) == repo.session_secret(store)


def test_file_store_persists(tmp_path):
    path = tmp_path / "keiri.json"
    s1 = FileStore(path)
    repo.create_user(s1, "a", None)
    assert [u["username"] for u in repo.list_users(FileStore(path))] == ["a"]


def test_ip_lockout_across_usernames(client):
    token = csrf_of(client)
    for n in range(20):
        client.post("/login", data={"username": f"user{n}", "password": "x", "csrf": token})
    r = client.post("/login", data={"username": "keiri", "password": "correct-horse-battery", "csrf": token})
    assert "上限" in r.text


def test_healthz_and_headers(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.text == "ok"
    assert r.headers["X-Robots-Tag"] == "noindex, nofollow"


# ---------------------------------------------------------------- Google ログイン

@pytest.fixture
def google_client(store, monkeypatch):
    monkeypatch.setattr(config, "AUTH", "google")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(config, "ALLOWED_DOMAIN", "inclue-llc.jp")
    monkeypatch.setattr(config, "ALLOWED_EMAILS", set())
    return TestClient(create_app(store=store, secret_key="test-secret"))


def _google_login(client, monkeypatch, **claim_overrides):
    r = client.get("/auth/google?next=/clients", follow_redirects=False)
    assert r.status_code == 307
    location = r.headers["location"]
    assert location.startswith(google_auth.AUTH_URL) and "hd=inclue-llc.jp" in location
    from urllib.parse import parse_qs, urlparse

    params = parse_qs(urlparse(location).query)
    assert params["redirect_uri"] == ["http://testserver/auth/callback"]
    claims = {
        "iss": "https://accounts.google.com", "aud": "cid.apps.googleusercontent.com", "exp": 9999999999,
        "nonce": params["nonce"][0], "email": "m_sato@inclue-llc.jp", "email_verified": True, "hd": "inclue-llc.jp",
        **claim_overrides,
    }
    monkeypatch.setattr(google_auth, "exchange_code", lambda *a: claims)
    return client.get(f"/auth/callback?code=x&state={params['state'][0]}", follow_redirects=False)


def test_google_login_page(google_client):
    page = google_client.get("/login").text
    assert "Google アカウントでログイン" in page and 'name="password"' not in page


def test_google_login_success(google_client, monkeypatch, store):
    r = _google_login(google_client, monkeypatch)
    assert r.status_code == 303 and r.headers["location"] == "/clients"
    assert google_client.get("/clients").status_code == 200
    assert [u["username"] for u in repo.list_users(store)] == ["m_sato@inclue-llc.jp"]
    # 2回目のログインでユーザーは増えない
    google_client.cookies.clear()
    _google_login(google_client, monkeypatch)
    assert len(repo.list_users(store)) == 1
    assert 'name="password"' not in google_client.get("/users").text


def test_google_login_rejects_other_domain(google_client, monkeypatch):
    r = _google_login(google_client, monkeypatch, email="someone@gmail.com", hd=None)
    assert r.status_code == 401 and "inclue-llc.jp のアカウント" in r.text
    assert google_client.get("/", follow_redirects=False).status_code == 303


def test_google_login_rejects_bad_state(google_client):
    google_client.get("/auth/google", follow_redirects=False)
    r = google_client.get("/auth/callback?code=x&state=wrong", follow_redirects=False)
    assert r.status_code == 401


def test_password_login_disabled_in_google_mode(google_client, store):
    repo.create_user(store, "keiri", auth.hash_password("correct-horse-battery"))
    google_client.get("/login")
    r = google_client.post(
        "/login", data={"username": "keiri", "password": "correct-horse-battery", "csrf": "x"},
        follow_redirects=False,
    )
    assert r.status_code in (303, 400)
    assert google_client.get("/", follow_redirects=False).status_code == 303


@pytest.mark.parametrize(
    "override, message",
    [
        ({"aud": "other"}, "発行元"),
        ({"iss": "https://evil.example"}, "発行元"),
        ({"exp": 1}, "有効期限"),
        ({"nonce": "other"}, "一致しません"),
        ({"email_verified": False}, "確認できません"),
        ({"hd": "other.jp", "email": "a@other.jp"}, "inclue-llc.jp"),
        ({"hd": "inclue-llc.jp", "email": "a@other.jp"}, "inclue-llc.jp"),
    ],
)
def test_validate_claims_rejects(override, message):
    claims = {
        "iss": "accounts.google.com", "aud": "cid", "exp": 2000, "nonce": "n",
        "email": "a@inclue-llc.jp", "email_verified": True, "hd": "inclue-llc.jp", **override,
    }
    with pytest.raises(google_auth.LoginError, match=message):
        google_auth.validate_claims(claims, "cid", "n", "inclue-llc.jp", set(), now=1000)


def test_validate_claims_allowlist():
    claims = {
        "iss": "accounts.google.com", "aud": "cid", "exp": 2000, "nonce": "n",
        "email": "A@inclue-llc.jp", "email_verified": True, "hd": "inclue-llc.jp",
    }
    assert google_auth.validate_claims(claims, "cid", "n", "inclue-llc.jp", {"a@inclue-llc.jp"}, now=1000) == "a@inclue-llc.jp"
    with pytest.raises(google_auth.LoginError, match="許可されていません"):
        google_auth.validate_claims(claims, "cid", "n", "inclue-llc.jp", {"b@inclue-llc.jp"}, now=1000)
