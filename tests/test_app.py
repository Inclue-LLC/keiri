from app import db
from tests.conftest import SAMPLES, csrf_of


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


def test_full_flow(logged_in, db_path):
    c = logged_in
    assert "新規4件" in _upload(c, "invoy", "invoy_invoices.csv").text
    assert "新規2件" in _upload(c, "smbc", "smbc_statement.csv").text
    assert "新規2件" in _upload(c, "gmo", "gmo_aozora_statement.csv").text
    # 同じ明細の再取込は重複しない
    assert "新規0件" in _upload(c, "smbc", "smbc_statement.csv").text

    page = c.get("/").text
    assert "要確認" in page and "期日超過" in page and "手数料差引の可能性あり" in page

    with db.transaction(db_path) as conn:
        inv = conn.execute("SELECT * FROM invoices WHERE invoice_no = '20260901-1'").fetchone()
        tx = conn.execute("SELECT * FROM transactions WHERE amount = 110000").fetchone()

    detail = c.get(f"/invoices/{inv['id']}").text
    assert "この入金で確定" in detail

    with db.transaction(db_path) as conn:
        memo_inv = conn.execute("SELECT id FROM invoices WHERE invoice_no = '20261001-1'").fetchone()[0]
    assert "11月分と合算して入金予定" in c.get(f"/invoices/{memo_inv}").text

    token = csrf_of(c, f"/invoices/{inv['id']}")
    r = c.post(
        f"/invoices/{inv['id']}/confirm",
        data={"transaction_id": tx["id"], "register_alias": "1", "note": "確認済", "csrf": token},
    )
    assert "入金済として確定しました" in r.text
    assert "確定済" in r.text

    with db.transaction(db_path) as conn:
        alias = conn.execute("SELECT alias FROM client_aliases WHERE client_id = ?", (inv["client_id"],)).fetchone()
        assert alias["alias"] == "カ)ヤマダシヨウジ"
        assert conn.execute("SELECT confirmed_by FROM matches").fetchone()[0] == 1

    # 消込済の入金を別の請求書に使うことはできない
    with db.transaction(db_path) as conn:
        other = conn.execute("SELECT id FROM invoices WHERE invoice_no = '20261001-1'").fetchone()[0]
    r = c.post(f"/invoices/{other}/confirm", data={"transaction_id": tx["id"], "csrf": token})
    assert "既に消込済" in r.text

    # 取消
    r = c.post(f"/invoices/{inv['id']}/unconfirm", data={"csrf": token})
    assert "確定を取り消しました" in r.text
    with db.transaction(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 0


def test_manual_confirm_requires_note(logged_in, db_path):
    _upload(logged_in, "invoy", "invoy_invoices.csv")
    with db.transaction(db_path) as conn:
        inv = conn.execute("SELECT id FROM invoices WHERE invoice_no = '20260820-1'").fetchone()[0]
    token = csrf_of(logged_in, f"/invoices/{inv}")
    r = logged_in.post(f"/invoices/{inv}/confirm-manual", data={"note": " ", "csrf": token})
    assert "理由" in r.text
    r = logged_in.post(f"/invoices/{inv}/confirm-manual", data={"note": "現金受領", "csrf": token})
    assert "明細なし" in r.text


def test_alias_crud(logged_in, db_path):
    _upload(logged_in, "invoy", "invoy_invoices.csv")
    with db.transaction(db_path) as conn:
        cid = conn.execute("SELECT id FROM clients WHERE name = '合同会社ABC'").fetchone()[0]
    token = csrf_of(logged_in, f"/clients/{cid}")
    r = logged_in.post(f"/clients/{cid}/aliases", data={"alias": "ｴｰﾋﾞｰｼｰﾎｰﾙﾃﾞｨﾝｸﾞｽ", "csrf": token})
    assert "登録しました" in r.text
    r = logged_in.post(f"/clients/{cid}/aliases", data={"alias": "エービーシーホールディングス", "csrf": token})
    assert "既に登録" in r.text
    with db.transaction(db_path) as conn:
        aid = conn.execute("SELECT id FROM client_aliases").fetchone()[0]
    r = logged_in.post(f"/clients/{cid}/aliases/{aid}/delete", data={"csrf": token})
    assert "削除しました" in r.text


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
    r = logged_in.post(
        "/account/password",
        data={"current_password": "correct-horse-battery", "new_password": "another-long-pw", "csrf": token},
    )
    assert "変更しました" in r.text


def test_combined_confirm_and_unconfirm(logged_in, db_path):
    with db.transaction(db_path) as conn:
        conn.execute("INSERT INTO clients (id, name) VALUES (1, '株式会社サンプル')")
        for no, amount in (("A", 440000), ("B", 165000)):
            conn.execute(
                "INSERT INTO invoices (invoice_no, client_id, issue_date, due_date, amount) "
                "VALUES (?, 1, '2026-04-01', '2026-05-31', ?)",
                (no, amount),
            )
        conn.execute(
            "INSERT INTO transactions (bank, date, amount, description, payer_kana, dedupe_key) "
            "VALUES ('smbc', '2026-05-29', 604560, '', 'カ)サンプル', 'k')"
        )
        a, b = [r[0] for r in conn.execute("SELECT id FROM invoices ORDER BY invoice_no")]
    page = logged_in.get(f"/invoices/{a}").text
    assert "まとめ入金の可能性" in page and "2件まとめて確定" in page

    token = csrf_of(logged_in, f"/invoices/{a}")
    r = logged_in.post(
        f"/invoices/{a}/confirm",
        data={"transaction_id": 1, "invoice_ids": [a, b], "register_alias": "1", "csrf": token},
    )
    assert "確定しました（2件）" in r.text
    with db.transaction(db_path) as conn:
        rows = conn.execute("SELECT invoice_id, fee_diff FROM matches ORDER BY invoice_id").fetchall()
        assert [tuple(r) for r in rows] == [(a, 440), (b, 0)]
    assert "まとめ入金" in logged_in.get(f"/invoices/{b}").text
    tx_page = logged_in.get("/transactions?filter=matched").text
    assert ">A</a> + <a" in tx_page

    r = logged_in.post(f"/invoices/{b}/unconfirm", data={"csrf": token})
    assert "確定を取り消しました（A, B）" in r.text
    with db.transaction(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 0


def test_migrates_old_unique_matches_table(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old_schema = db.SCHEMA.replace(
        "transaction_id INTEGER REFERENCES transactions(id),",
        "transaction_id INTEGER UNIQUE REFERENCES transactions(id),",
    ).replace("    memo TEXT NOT NULL DEFAULT '',\n", "")
    conn = sqlite3.connect(path)
    conn.executescript(old_schema)
    conn.execute("INSERT INTO users (id, username, password_hash) VALUES (1, 'u', 'x')")
    conn.execute("INSERT INTO clients (id, name) VALUES (1, 'c')")
    conn.execute("INSERT INTO invoices (id, invoice_no, client_id, amount) VALUES (1, 'A', 1, 100), (2, 'B', 1, 100)")
    conn.execute(
        "INSERT INTO transactions (id, bank, date, amount, description, payer_kana, dedupe_key) "
        "VALUES (1, 'smbc', '2026-01-01', 200, '', '', 'k')"
    )
    conn.execute("INSERT INTO matches (invoice_id, transaction_id, confirmed_by) VALUES (1, 1, 1)")
    conn.commit()
    conn.close()

    db.init_db(path)
    with db.transaction(path) as conn:
        conn.execute("INSERT INTO matches (invoice_id, transaction_id, confirmed_by) VALUES (2, 1, 1)")
        assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 2
        assert "memo" in {r["name"] for r in conn.execute("PRAGMA table_info(invoices)")}


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


def test_initial_user_from_env(tmp_path, monkeypatch):
    from app import config
    from app.main import create_app

    monkeypatch.setattr(config, "INITIAL_USER", "admin")
    monkeypatch.setattr(config, "INITIAL_PASSWORD", "initial-password")
    path = tmp_path / "t.db"
    create_app(db_path=path, secret_key="s")
    create_app(db_path=path, secret_key="s")  # 2回目の起動では作り直さない
    with db.transaction(path) as conn:
        assert [r[0] for r in conn.execute("SELECT username FROM users")] == ["admin"]

    monkeypatch.setattr(config, "INITIAL_USER", "other")
    create_app(db_path=path, secret_key="s")
    with db.transaction(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_initial_user_rejects_short_password(tmp_path, monkeypatch):
    import pytest
    from app import config
    from app.main import create_app

    monkeypatch.setattr(config, "INITIAL_USER", "admin")
    monkeypatch.setattr(config, "INITIAL_PASSWORD", "short")
    with pytest.raises(RuntimeError):
        create_app(db_path=tmp_path / "t.db", secret_key="s")


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
