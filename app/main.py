"""入金消込 Web アプリ本体。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, config, csv_import, db, kana, matching

BASE_DIR = Path(__file__).resolve().parent
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
IMPORT_KINDS = {"invoy": "INVOY 請求書", **csv_import.BANKS}

templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.filters["yen"] = lambda v: "" if v is None else f"{v:,}"


class LoginRequired(Exception):
    pass


class CsrfError(Exception):
    pass


def create_app(db_path: Path | None = None, secret_key: str | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db_path = Path(db_path or config.DB_PATH)
    db.init_db(app.state.db_path)

    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key or config.secret_key(),
        session_cookie="keiri_session",
        max_age=config.SESSION_MAX_AGE,
        same_site="lax",
        https_only=config.COOKIE_SECURE,
    )
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)

    @app.exception_handler(CsrfError)
    async def _csrf_error(request: Request, exc: CsrfError):
        return Response("不正なリクエストです（ページを再読み込みしてやり直してください）", status_code=400)

    _register_routes(app)
    return app


# ---------------------------------------------------------------- helpers

def _conn(request: Request):
    return db.transaction(request.app.state.db_path)


def current_user(request: Request) -> sqlite3.Row:
    uid = request.session.get("user_id")
    if not uid:
        raise LoginRequired()
    with _conn(request) as conn:
        user = conn.execute("SELECT id, username FROM users WHERE id = ?", (uid,)).fetchone()
    if not user:
        request.session.clear()
        raise LoginRequired()
    return user


def require_csrf(request: Request, token: str) -> None:
    if not auth.check_csrf(request.session, token):
        raise CsrfError()


def flash(request: Request, message: str, kind: str = "info") -> None:
    request.session.setdefault("flash", []).append([kind, message])


def render(request: Request, name: str, user=None, status_code: int = 200, **ctx) -> HTMLResponse:
    ctx.update(
        user=user,
        csrf=auth.csrf_token(request.session),
        flashes=request.session.pop("flash", []),
        bank_names=csv_import.BANKS,
        fee_tolerance=config.FEE_TOLERANCE,
    )
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _safe_next(url: str | None) -> str:
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return "/"


# ---------------------------------------------------------------- import

def import_invoices(conn: sqlite3.Connection, parsed: csv_import.ParseResult) -> tuple[int, int, int]:
    added = updated = skipped = 0
    for inv in parsed.rows:
        conn.execute("INSERT OR IGNORE INTO clients (name) VALUES (?)", (inv.client_name,))
        client = conn.execute("SELECT * FROM clients WHERE name = ?", (inv.client_name,)).fetchone()
        if inv.client_kana and not client["name_kana"]:
            conn.execute("UPDATE clients SET name_kana = ? WHERE id = ?", (inv.client_kana, client["id"]))
        existing = conn.execute(
            "SELECT i.id, m.id AS match_id FROM invoices i LEFT JOIN matches m ON m.invoice_id = i.id "
            "WHERE i.invoice_no = ?",
            (inv.invoice_no,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO invoices (invoice_no, client_id, subject, issue_date, due_date, amount, memo) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (inv.invoice_no, client["id"], inv.subject, inv.issue_date, inv.due_date, inv.amount, inv.memo),
            )
            added += 1
        elif existing["match_id"] is None:
            conn.execute(
                "UPDATE invoices SET client_id = ?, subject = ?, issue_date = ?, due_date = ?, amount = ?, memo = ? "
                "WHERE id = ?",
                (client["id"], inv.subject, inv.issue_date, inv.due_date, inv.amount, inv.memo, existing["id"]),
            )
            updated += 1
        else:
            # 確定済みの請求書は上書きしない
            skipped += 1
    return added, updated, skipped


def import_transactions(conn: sqlite3.Connection, parsed: csv_import.ParseResult) -> tuple[int, int]:
    added = 0
    for t in parsed.rows:
        cur = conn.execute(
            "INSERT OR IGNORE INTO transactions (bank, date, amount, description, payer_kana, balance, dedupe_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (t.bank, t.date, t.amount, t.description, t.payer_kana, t.balance, t.dedupe_key),
        )
        added += cur.rowcount
    return added, len(parsed.rows) - added


# ---------------------------------------------------------------- routes

def _register_routes(app: FastAPI) -> None:

    # --- 認証 ---

    @app.get("/login")
    def login_form(request: Request, next: str = "/"):
        return render(request, "login.html", next=_safe_next(next))

    @app.post("/login")
    def login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        next: str = Form("/"),
        csrf: str = Form(""),
    ):
        require_csrf(request, csrf)
        username = username.strip()
        error = None
        if auth.is_locked(username):
            error = "ログイン試行回数が上限に達しました。しばらく待ってから再度お試しください"
        else:
            with _conn(request) as conn:
                user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
                if user and auth.verify_password(password, user["password_hash"]):
                    auth.clear_failures(username)
                    request.session.clear()
                    request.session["user_id"] = user["id"]
                    db.audit(conn, user["id"], "login")
                    return redirect(_safe_next(next))
            auth.record_failure(username)
            error = "ユーザー名またはパスワードが違います"
        return render(request, "login.html", next=_safe_next(next), error=error, status_code=401)

    @app.post("/logout")
    def logout(request: Request, csrf: str = Form("")):
        require_csrf(request, csrf)
        request.session.clear()
        return redirect("/login")

    # --- 一覧 ---

    @app.get("/")
    def index(request: Request, status: str = "", q: str = ""):
        user = current_user(request)
        with _conn(request) as conn:
            views = matching.build_views(conn)
        counts = {s: 0 for s in matching.STATUSES}
        for v in views:
            counts[v.status] += 1
        if status in matching.STATUSES:
            views = [v for v in views if v.status == status]
        if q:
            ql = q.lower()
            views = [
                v for v in views
                if ql in v.invoice["invoice_no"].lower()
                or ql in v.invoice["client_name"].lower()
                or ql in (v.invoice["subject"] or "").lower()
            ]
        return render(
            request, "index.html", user,
            views=views, counts=counts, statuses=matching.STATUSES, status=status, q=q,
        )

    # --- CSV 取込 ---

    @app.get("/upload")
    def upload_form(request: Request):
        user = current_user(request)
        with _conn(request) as conn:
            history = conn.execute(
                "SELECT im.*, u.username FROM imports im JOIN users u ON u.id = im.imported_by "
                "ORDER BY im.id DESC LIMIT 20"
            ).fetchall()
        return render(request, "upload.html", user, kinds=IMPORT_KINDS, history=history)

    @app.post("/upload")
    async def upload(
        request: Request,
        kind: str = Form(...),
        file: UploadFile = File(...),
        csrf: str = Form(""),
    ):
        user = current_user(request)
        require_csrf(request, csrf)
        if kind not in IMPORT_KINDS:
            flash(request, "取込種別が不正です", "error")
            return redirect("/upload")
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            flash(request, "ファイルが大きすぎます（10MBまで）", "error")
            return redirect("/upload")
        try:
            parsed = csv_import.parse_invoy(data) if kind == "invoy" else csv_import.parse_bank(data, kind)
        except csv_import.CsvFormatError as e:
            flash(request, f"{file.filename}: {e}", "error")
            return redirect("/upload")

        with db.transaction(request.app.state.db_path) as conn:
            if kind == "invoy":
                added, updated, skipped = import_invoices(conn, parsed)
                msg = f"請求書 新規{added}件・更新{updated}件・確定済のためスキップ{skipped}件"
            else:
                added, skipped = import_transactions(conn, parsed)
                msg = f"{IMPORT_KINDS[kind]} 入金 新規{added}件・取込済のためスキップ{skipped}件"
            skipped += parsed.skipped
            conn.execute(
                "INSERT INTO imports (kind, filename, added, skipped, imported_by) VALUES (?, ?, ?, ?, ?)",
                (kind, file.filename or "", added, skipped, user["id"]),
            )
            db.audit(conn, user["id"], "import", f"{kind} {file.filename}: {msg}")
        flash(request, f"{file.filename}: {msg}（対象外の行 {parsed.skipped}件）", "success")
        for w in parsed.warnings[:20]:
            flash(request, w, "warn")
        return redirect("/upload")

    # --- 請求書詳細・消込 ---

    @app.get("/invoices/{invoice_id}")
    def invoice_detail(request: Request, invoice_id: int, tx_q: str = ""):
        user = current_user(request)
        with _conn(request) as conn:
            view = next((v for v in matching.build_views(conn) if v.invoice["id"] == invoice_id), None)
            if view is None:
                return render(request, "not_found.html", user, status_code=404)
            matched_tx = confirmed_by = None
            if view.match:
                if view.match["transaction_id"]:
                    matched_tx = conn.execute(
                        "SELECT * FROM transactions WHERE id = ?", (view.match["transaction_id"],)
                    ).fetchone()
                confirmed_by = conn.execute(
                    "SELECT username FROM users WHERE id = ?", (view.match["confirmed_by"],)
                ).fetchone()["username"]
            aliases = conn.execute(
                "SELECT * FROM client_aliases WHERE client_id = ? ORDER BY id", (view.invoice["client_id"],)
            ).fetchall()
            search_results = []
            if tx_q and not view.match:
                search_results = _search_open_transactions(conn, tx_q)
        return render(
            request, "invoice.html", user,
            view=view, matched_tx=matched_tx, confirmed_by=confirmed_by, aliases=aliases,
            tx_q=tx_q, search_results=search_results,
        )

    @app.post("/invoices/{invoice_id}/confirm")
    def confirm(
        request: Request,
        invoice_id: int,
        transaction_id: int = Form(...),
        note: str = Form(""),
        register_alias: str = Form(""),
        csrf: str = Form(""),
    ):
        user = current_user(request)
        require_csrf(request, csrf)
        url = f"/invoices/{invoice_id}"
        with db.transaction(request.app.state.db_path) as conn:
            inv = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
            tx = conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
            if not inv or not tx:
                flash(request, "請求書または入金明細が見つかりません", "error")
                return redirect(url)
            try:
                conn.execute(
                    "INSERT INTO matches (invoice_id, transaction_id, fee_diff, note, confirmed_by) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (invoice_id, transaction_id, inv["amount"] - tx["amount"], note.strip(), user["id"]),
                )
            except sqlite3.IntegrityError:
                flash(request, "この請求書または入金は既に消込済です", "error")
                return redirect(url)
            if register_alias and tx["payer_kana"]:
                _add_alias(conn, inv["client_id"], tx["payer_kana"])
            db.audit(
                conn, user["id"], "confirm",
                f"請求書 {inv['invoice_no']} ← 入金#{tx['id']} {tx['date']} {tx['amount']:,}円 {tx['payer_kana']}",
            )
        flash(request, "入金済として確定しました", "success")
        return redirect(url)

    @app.post("/invoices/{invoice_id}/confirm-manual")
    def confirm_manual(request: Request, invoice_id: int, note: str = Form(""), csrf: str = Form("")):
        user = current_user(request)
        require_csrf(request, csrf)
        url = f"/invoices/{invoice_id}"
        if not note.strip():
            flash(request, "明細なしで確定する場合は理由（メモ）を入力してください", "error")
            return redirect(url)
        with db.transaction(request.app.state.db_path) as conn:
            inv = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
            if not inv:
                return redirect("/")
            try:
                conn.execute(
                    "INSERT INTO matches (invoice_id, transaction_id, note, confirmed_by) VALUES (?, NULL, ?, ?)",
                    (invoice_id, note.strip(), user["id"]),
                )
            except sqlite3.IntegrityError:
                flash(request, "この請求書は既に消込済です", "error")
                return redirect(url)
            db.audit(conn, user["id"], "confirm_manual", f"請求書 {inv['invoice_no']}: {note.strip()}")
        flash(request, "入金済として確定しました（明細なし）", "success")
        return redirect(url)

    @app.post("/invoices/{invoice_id}/unconfirm")
    def unconfirm(request: Request, invoice_id: int, csrf: str = Form("")):
        user = current_user(request)
        require_csrf(request, csrf)
        with db.transaction(request.app.state.db_path) as conn:
            inv = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
            cur = conn.execute("DELETE FROM matches WHERE invoice_id = ?", (invoice_id,))
            if inv and cur.rowcount:
                db.audit(conn, user["id"], "unconfirm", f"請求書 {inv['invoice_no']}")
        flash(request, "確定を取り消しました", "success")
        return redirect(f"/invoices/{invoice_id}")

    # --- 入金明細 ---

    @app.get("/transactions")
    def transactions(request: Request, filter: str = "open", q: str = ""):
        user = current_user(request)
        sql = (
            "SELECT t.*, i.id AS invoice_id, i.invoice_no FROM transactions t "
            "LEFT JOIN matches m ON m.transaction_id = t.id LEFT JOIN invoices i ON i.id = m.invoice_id "
        )
        where, params = [], []
        if filter == "open":
            where.append("m.id IS NULL")
        elif filter == "matched":
            where.append("m.id IS NOT NULL")
        if q:
            where.append("(t.payer_kana LIKE ? OR t.description LIKE ? OR CAST(t.amount AS TEXT) = ?)")
            params += [f"%{q}%", f"%{q}%", q.replace(",", "")]
        if where:
            sql += "WHERE " + " AND ".join(where) + " "
        sql += "ORDER BY t.date DESC, t.id DESC LIMIT 500"
        with _conn(request) as conn:
            rows = conn.execute(sql, params).fetchall()
        return render(request, "transactions.html", user, rows=rows, filter=filter, q=q)

    # --- 取引先・振込名義の別名 ---

    @app.get("/clients")
    def clients(request: Request):
        user = current_user(request)
        with _conn(request) as conn:
            rows = conn.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM invoices i WHERE i.client_id = c.id) AS invoice_count, "
                "(SELECT GROUP_CONCAT(alias, ' / ') FROM client_aliases a WHERE a.client_id = c.id) AS aliases "
                "FROM clients c ORDER BY c.name"
            ).fetchall()
        return render(request, "clients.html", user, rows=rows)

    @app.get("/clients/{client_id}")
    def client_detail(request: Request, client_id: int):
        user = current_user(request)
        with _conn(request) as conn:
            client = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
            if not client:
                return render(request, "not_found.html", user, status_code=404)
            aliases = conn.execute(
                "SELECT * FROM client_aliases WHERE client_id = ? ORDER BY id", (client_id,)
            ).fetchall()
            invoices = conn.execute(
                "SELECT i.*, m.id AS match_id FROM invoices i LEFT JOIN matches m ON m.invoice_id = i.id "
                "WHERE i.client_id = ? ORDER BY i.issue_date DESC LIMIT 50",
                (client_id,),
            ).fetchall()
        return render(request, "client.html", user, client=client, aliases=aliases, invoices=invoices)

    @app.post("/clients/{client_id}/kana")
    def client_kana(request: Request, client_id: int, name_kana: str = Form(""), csrf: str = Form("")):
        user = current_user(request)
        require_csrf(request, csrf)
        with db.transaction(request.app.state.db_path) as conn:
            conn.execute("UPDATE clients SET name_kana = ? WHERE id = ?", (name_kana.strip(), client_id))
            db.audit(conn, user["id"], "client_kana", f"取引先#{client_id}: {name_kana.strip()}")
        flash(request, "カナ名を更新しました", "success")
        return redirect(f"/clients/{client_id}")

    @app.post("/clients/{client_id}/aliases")
    def add_alias(request: Request, client_id: int, alias: str = Form(...), csrf: str = Form("")):
        user = current_user(request)
        require_csrf(request, csrf)
        alias = alias.strip()
        if not kana.normalize(alias):
            flash(request, "別名を入力してください", "error")
            return redirect(f"/clients/{client_id}")
        with db.transaction(request.app.state.db_path) as conn:
            if _add_alias(conn, client_id, alias):
                db.audit(conn, user["id"], "alias_add", f"取引先#{client_id}: {alias}")
                flash(request, f"別名「{alias}」を登録しました", "success")
            else:
                flash(request, "同じ別名が既に登録されています", "warn")
        return redirect(f"/clients/{client_id}")

    @app.post("/clients/{client_id}/aliases/{alias_id}/delete")
    def delete_alias(request: Request, client_id: int, alias_id: int, csrf: str = Form("")):
        user = current_user(request)
        require_csrf(request, csrf)
        with db.transaction(request.app.state.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM client_aliases WHERE id = ? AND client_id = ?", (alias_id, client_id)
            ).fetchone()
            if row:
                conn.execute("DELETE FROM client_aliases WHERE id = ?", (alias_id,))
                db.audit(conn, user["id"], "alias_delete", f"取引先#{client_id}: {row['alias']}")
        flash(request, "別名を削除しました", "success")
        return redirect(f"/clients/{client_id}")

    # --- ユーザー管理 ---

    @app.get("/users")
    def users(request: Request):
        user = current_user(request)
        with _conn(request) as conn:
            rows = conn.execute("SELECT id, username, created_at FROM users ORDER BY id").fetchall()
            logs = conn.execute(
                "SELECT l.*, u.username FROM audit_log l LEFT JOIN users u ON u.id = l.user_id "
                "ORDER BY l.id DESC LIMIT 100"
            ).fetchall()
        return render(request, "users.html", user, rows=rows, logs=logs)

    @app.post("/users")
    def create_user(request: Request, username: str = Form(...), password: str = Form(...), csrf: str = Form("")):
        user = current_user(request)
        require_csrf(request, csrf)
        username = username.strip()
        err = auth.validate_new_password(password) or (None if username else "ユーザー名を入力してください")
        if err:
            flash(request, err, "error")
            return redirect("/users")
        with db.transaction(request.app.state.db_path) as conn:
            try:
                conn.execute(
                    "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                    (username, auth.hash_password(password)),
                )
            except sqlite3.IntegrityError:
                flash(request, "そのユーザー名は既に使われています", "error")
                return redirect("/users")
            db.audit(conn, user["id"], "user_create", username)
        flash(request, f"ユーザー「{username}」を作成しました", "success")
        return redirect("/users")

    @app.post("/account/password")
    def change_password(
        request: Request,
        current_password: str = Form(...),
        new_password: str = Form(...),
        csrf: str = Form(""),
    ):
        user = current_user(request)
        require_csrf(request, csrf)
        with db.transaction(request.app.state.db_path) as conn:
            row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
            if not auth.verify_password(current_password, row["password_hash"]):
                flash(request, "現在のパスワードが違います", "error")
                return redirect("/users")
            err = auth.validate_new_password(new_password)
            if err:
                flash(request, err, "error")
                return redirect("/users")
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?", (auth.hash_password(new_password), user["id"])
            )
            db.audit(conn, user["id"], "password_change")
        flash(request, "パスワードを変更しました", "success")
        return redirect("/users")


def _add_alias(conn: sqlite3.Connection, client_id: int, alias: str) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO client_aliases (client_id, alias, alias_norm) VALUES (?, ?, ?)",
        (client_id, alias, kana.normalize(alias)),
    )
    return cur.rowcount > 0


def _search_open_transactions(conn: sqlite3.Connection, q: str) -> list[sqlite3.Row]:
    amount = q.replace(",", "").strip()
    return conn.execute(
        "SELECT t.* FROM transactions t LEFT JOIN matches m ON m.transaction_id = t.id "
        "WHERE m.id IS NULL AND (t.payer_kana LIKE ? OR t.description LIKE ? OR CAST(t.amount AS TEXT) = ?) "
        "ORDER BY t.date DESC LIMIT 50",
        (f"%{q}%", f"%{q}%", amount),
    ).fetchall()

