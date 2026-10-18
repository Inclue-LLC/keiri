"""入金消込 Web アプリ本体。"""
from __future__ import annotations

import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, config, csv_import, google_auth, kana, matching, repo
from .store import FileStore, FirestoreStore, Store

BASE_DIR = Path(__file__).resolve().parent
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
IMPORT_KINDS = {"invoy": "INVOY 請求書", **csv_import.BANKS}

templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.filters["yen"] = lambda v: "" if v is None else f"{v:,}"


class LoginRequired(Exception):
    pass


class CsrfError(Exception):
    pass


def make_store() -> Store:
    if config.STORE == "firestore":
        return FirestoreStore()
    return FileStore(config.STORE_PATH)


def create_app(store: Store | None = None, secret_key: str | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store or make_store()
    if config.AUTH == "password":
        _create_initial_user(app.state.store)

    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key or config.SECRET_KEY or repo.session_secret(app.state.store),
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
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        if config.COOKIE_SECURE:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)

    @app.exception_handler(CsrfError)
    async def _csrf_error(request: Request, exc: CsrfError):
        return Response("不正なリクエストです（ページを再読み込みしてやり直してください）", status_code=400)

    _register_routes(app)
    return app


def _create_initial_user(store: Store) -> None:
    """サーバー上でコマンドを実行できない環境向けに、ユーザーが1人もいなければ環境変数から作成する。"""
    username = config.INITIAL_USER.strip()
    password = config.INITIAL_PASSWORD
    if not username or not password:
        return
    err = auth.validate_new_password(password)
    if err:
        raise RuntimeError(f"KEIRI_INITIAL_PASSWORD: {err}")
    if repo.list_users(store):
        return
    if repo.create_user(store, username, auth.hash_password(password)):
        repo.audit(store, repo.user_id_for(username), "user_create", f"{username}（初期ユーザー）")


# ---------------------------------------------------------------- helpers

def _store(request: Request) -> Store:
    return request.app.state.store


def current_user(request: Request) -> dict:
    uid = request.session.get("user_id")
    if not uid:
        raise LoginRequired()
    user = repo.get_user(_store(request), uid)
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
        bank_formats=csv_import.BANK_CSV_FORMATS,
        fee_tolerance=config.FEE_TOLERANCE,
        auth_mode=config.AUTH,
        allowed_domain=config.ALLOWED_DOMAIN,
    )
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _safe_next(url: str | None) -> str:
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return "/"


def _login(request: Request, user: dict, next_url: str) -> RedirectResponse:
    request.session.clear()
    request.session["user_id"] = user["id"]
    repo.audit(_store(request), user["id"], "login")
    return redirect(_safe_next(next_url))


def _google_redirect_uri(request: Request) -> str:
    return str(request.url_for("google_callback"))


# ---------------------------------------------------------------- routes

def _register_routes(app: FastAPI) -> None:

    @app.get("/healthz")
    def healthz():
        return Response("ok", media_type="text/plain")

    # --- 認証 ---

    @app.get("/login")
    def login_form(request: Request, next: str = "/"):
        return render(
            request, "login.html", next=_safe_next(next),
            google_ready=bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET),
        )

    @app.post("/login")
    def login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        next: str = Form("/"),
        csrf: str = Form(""),
    ):
        require_csrf(request, csrf)
        if config.AUTH != "password":
            return redirect("/login")
        username = username.strip()
        ip = request.client.host if request.client else ""
        if auth.is_locked(username, ip):
            error = "ログイン試行回数が上限に達しました。しばらく待ってから再度お試しください"
        else:
            user = repo.find_user(_store(request), username)
            if user and user.get("password_hash") and auth.verify_password(password, user["password_hash"]):
                auth.clear_failures(username)
                return _login(request, user, next)
            auth.record_failure(username, ip)
            error = "ユーザー名またはパスワードが違います"
        return render(request, "login.html", next=_safe_next(next), error=error, status_code=401)

    @app.get("/auth/google")
    def google_login(request: Request, next: str = "/"):
        if config.AUTH != "google" or not config.GOOGLE_CLIENT_ID:
            return redirect("/login")
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        request.session["oauth"] = {"state": state, "nonce": nonce, "next": _safe_next(next)}
        return RedirectResponse(google_auth.authorization_url(
            config.GOOGLE_CLIENT_ID, _google_redirect_uri(request), state, nonce, config.ALLOWED_DOMAIN,
        ))

    @app.get("/auth/callback", name="google_callback")
    def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        pending = request.session.pop("oauth", None)
        try:
            if config.AUTH != "google":
                raise google_auth.LoginError("Google ログインは無効です")
            if error or not code:
                raise google_auth.LoginError("ログインがキャンセルされました")
            if not pending or not secrets.compare_digest(pending["state"], state):
                raise google_auth.LoginError("ログイン要求が一致しません。もう一度お試しください")
            claims = google_auth.exchange_code(
                code, config.GOOGLE_CLIENT_ID, config.GOOGLE_CLIENT_SECRET, _google_redirect_uri(request)
            )
            email = google_auth.validate_claims(
                claims, config.GOOGLE_CLIENT_ID, pending["nonce"], config.ALLOWED_DOMAIN, config.ALLOWED_EMAILS
            )
        except google_auth.LoginError as e:
            return render(request, "login.html", next="/", error=str(e), google_ready=True, status_code=401)
        user = repo.ensure_user(_store(request), email)
        return _login(request, user, pending["next"])

    @app.post("/logout")
    def logout(request: Request, csrf: str = Form("")):
        require_csrf(request, csrf)
        request.session.clear()
        return redirect("/login")

    # --- 一覧 ---

    @app.get("/")
    def index(request: Request, status: str = "", q: str = ""):
        user = current_user(request)
        views = matching.build_views(repo.load(_store(request)))
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
        store = _store(request)
        users = {u["id"]: u for u in repo.list_users(store)}
        history = [
            {**h, "username": users.get(h["imported_by"], {}).get("username", "")}
            for h in repo.latest(store, "imports", 20)
        ]
        return render(request, "upload.html", user, kinds=IMPORT_KINDS, history=history)

    @app.post("/upload")
    async def upload(
        request: Request,
        kind: str = Form(...),
        file: list[UploadFile] = File(...),
        csrf: str = Form(""),
    ):
        user = current_user(request)
        require_csrf(request, csrf)
        if kind not in IMPORT_KINDS:
            flash(request, "取込種別が不正です", "error")
            return redirect("/upload")
        for f in file:
            await _import_file(request, user, kind, f)
        return redirect("/upload")

    # --- 請求書詳細・消込 ---

    @app.get("/invoices/{invoice_id}")
    def invoice_detail(request: Request, invoice_id: str, tx_q: str = ""):
        user = current_user(request)
        snap = repo.load(_store(request))
        view = next((v for v in matching.build_views(snap) if v.invoice["id"] == invoice_id), None)
        if view is None:
            return render(request, "not_found.html", user, status_code=404)
        matched_tx = confirmed_by = None
        group_invoices = []
        if view.match:
            if view.match.get("transaction_id"):
                matched_tx = next((t for t in snap.transactions if t["id"] == view.match["transaction_id"]), None)
                by_id = snap.invoices_by_id
                group_invoices = sorted(
                    (by_id[i] for i in (matched_tx or {}).get("invoice_ids", []) if i in by_id),
                    key=lambda i: i["invoice_no"],
                )
            confirmed_by = snap.users.get(view.match["confirmed_by"], {}).get("username", "")
        search_results = repo.search_open_transactions(snap, tx_q) if tx_q and not view.match else []
        return render(
            request, "invoice.html", user,
            view=view, matched_tx=matched_tx, confirmed_by=confirmed_by,
            aliases=snap.aliases.get(view.invoice["client_id"], []),
            group_invoices=group_invoices if len(group_invoices) > 1 else [],
            tx_q=tx_q, search_results=search_results,
        )

    @app.post("/invoices/{invoice_id}/confirm")
    def confirm(
        request: Request,
        invoice_id: str,
        transaction_id: str = Form(...),
        invoice_ids: list[str] = Form([]),
        note: str = Form(""),
        register_alias: str = Form(""),
        csrf: str = Form(""),
    ):
        """入金を請求書に消し込む。invoice_ids を渡すとまとめ入金として複数の請求書を同時に確定する。"""
        user = current_user(request)
        require_csrf(request, csrf)
        ids = [invoice_id] + [i for i in dict.fromkeys(invoice_ids) if i != invoice_id]
        try:
            invs, _ = repo.confirm(_store(request), ids, transaction_id, note.strip(), user["id"], bool(register_alias))
        except repo.ConfirmError as e:
            flash(request, str(e), "error")
        else:
            flash(request, f"入金済として確定しました（{len(invs)}件）" if len(invs) > 1 else "入金済として確定しました", "success")
        return redirect(f"/invoices/{invoice_id}")

    @app.post("/invoices/{invoice_id}/confirm-manual")
    def confirm_manual(request: Request, invoice_id: str, note: str = Form(""), csrf: str = Form("")):
        user = current_user(request)
        require_csrf(request, csrf)
        if not note.strip():
            flash(request, "明細なしで確定する場合は理由（メモ）を入力してください", "error")
        else:
            try:
                repo.confirm_manual(_store(request), invoice_id, note.strip(), user["id"])
            except repo.ConfirmError as e:
                flash(request, str(e), "error")
            else:
                flash(request, "入金済として確定しました（明細なし）", "success")
        return redirect(f"/invoices/{invoice_id}")

    @app.post("/invoices/{invoice_id}/unconfirm")
    def unconfirm(request: Request, invoice_id: str, csrf: str = Form("")):
        user = current_user(request)
        require_csrf(request, csrf)
        nos = repo.unconfirm(_store(request), invoice_id, user["id"])
        if nos:
            flash(request, f"確定を取り消しました（{', '.join(nos)}）", "success")
        return redirect(f"/invoices/{invoice_id}")

    # --- 入金明細 ---

    @app.get("/transactions")
    def transactions(request: Request, filter: str = "open", q: str = ""):
        user = current_user(request)
        rows = repo.list_transactions(repo.load(_store(request)), filter, q.strip())
        return render(request, "transactions.html", user, rows=rows, filter=filter, q=q)

    # --- 取引先・振込名義の別名 ---

    @app.get("/clients")
    def clients(request: Request):
        user = current_user(request)
        snap = repo.load(_store(request))
        counts: dict[str, int] = {}
        for inv in snap.invoices:
            counts[inv["client_id"]] = counts.get(inv["client_id"], 0) + 1
        rows = sorted(
            (
                {**c, "invoice_count": counts.get(cid, 0),
                 "aliases": " / ".join(a["alias"] for a in snap.aliases.get(cid, []))}
                for cid, c in snap.clients.items()
            ),
            key=lambda c: c["name"],
        )
        return render(request, "clients.html", user, rows=rows)

    @app.get("/clients/{client_id}")
    def client_detail(request: Request, client_id: str):
        user = current_user(request)
        snap = repo.load(_store(request))
        client = snap.clients.get(client_id)
        if not client:
            return render(request, "not_found.html", user, status_code=404)
        invoices = sorted(
            ({**i, "match_id": i["id"] if i["id"] in snap.matches else None}
             for i in snap.invoices if i["client_id"] == client_id),
            key=lambda i: i.get("issue_date") or "", reverse=True,
        )[:50]
        return render(
            request, "client.html", user,
            client=client, aliases=snap.aliases.get(client_id, []), invoices=invoices,
        )

    @app.post("/clients/{client_id}/kana")
    def client_kana(request: Request, client_id: str, name_kana: str = Form(""), csrf: str = Form("")):
        user = current_user(request)
        require_csrf(request, csrf)
        repo.set_client_kana(_store(request), client_id, name_kana.strip())
        repo.audit(_store(request), user["id"], "client_kana", f"取引先 {client_id}: {name_kana.strip()}")
        flash(request, "カナ名を更新しました", "success")
        return redirect(f"/clients/{client_id}")

    @app.post("/clients/{client_id}/aliases")
    def add_alias(request: Request, client_id: str, alias: str = Form(...), csrf: str = Form("")):
        user = current_user(request)
        require_csrf(request, csrf)
        alias = alias.strip()
        if not kana.normalize(alias):
            flash(request, "別名を入力してください", "error")
        elif repo.add_alias(_store(request), client_id, alias):
            repo.audit(_store(request), user["id"], "alias_add", f"取引先 {client_id}: {alias}")
            flash(request, f"別名「{alias}」を登録しました", "success")
        else:
            flash(request, "同じ別名が既に登録されています", "warn")
        return redirect(f"/clients/{client_id}")

    @app.post("/clients/{client_id}/aliases/{alias_id}/delete")
    def delete_alias(request: Request, client_id: str, alias_id: str, csrf: str = Form("")):
        user = current_user(request)
        require_csrf(request, csrf)
        doc = repo.delete_alias(_store(request), client_id, alias_id)
        if doc:
            repo.audit(_store(request), user["id"], "alias_delete", f"取引先 {client_id}: {doc['alias']}")
        flash(request, "別名を削除しました", "success")
        return redirect(f"/clients/{client_id}")

    # --- ユーザー管理 ---

    @app.get("/users")
    def users(request: Request):
        user = current_user(request)
        store = _store(request)
        rows = repo.list_users(store)
        names = {u["id"]: u["username"] for u in rows}
        logs = [{**l, "username": names.get(l.get("user_id"), "")} for l in repo.latest(store, "audit_log", 100)]
        return render(request, "users.html", user, rows=rows, logs=logs)

    @app.post("/users")
    def create_user(request: Request, username: str = Form(...), password: str = Form(...), csrf: str = Form("")):
        user = current_user(request)
        require_csrf(request, csrf)
        if config.AUTH != "password":
            return redirect("/users")
        username = username.strip()
        err = auth.validate_new_password(password) or (None if username else "ユーザー名を入力してください")
        if err:
            flash(request, err, "error")
        elif not repo.create_user(_store(request), username, auth.hash_password(password)):
            flash(request, "そのユーザー名は既に使われています", "error")
        else:
            repo.audit(_store(request), user["id"], "user_create", username)
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
        if config.AUTH != "password":
            return redirect("/users")
        if not user.get("password_hash") or not auth.verify_password(current_password, user["password_hash"]):
            flash(request, "現在のパスワードが違います", "error")
        elif err := auth.validate_new_password(new_password):
            flash(request, err, "error")
        else:
            repo.set_password(_store(request), user["id"], auth.hash_password(new_password))
            repo.audit(_store(request), user["id"], "password_change")
            flash(request, "パスワードを変更しました", "success")
        return redirect("/users")


async def _import_file(request: Request, user: dict, kind: str, file: UploadFile) -> None:
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        flash(request, f"{file.filename}: ファイルが大きすぎます（10MBまで）", "error")
        return
    try:
        parsed = csv_import.parse_invoy(data) if kind == "invoy" else csv_import.parse_bank(data, kind)
    except csv_import.CsvFormatError as e:
        flash(request, f"{file.filename}: {e}", "error")
        return

    store = _store(request)
    if kind == "invoy":
        added, updated, skipped = repo.import_invoices(store, parsed)
        msg = f"請求書 新規{added}件・更新{updated}件・確定済のためスキップ{skipped}件"
    else:
        added, skipped = repo.import_transactions(store, parsed)
        msg = f"{IMPORT_KINDS[kind]} 入金 新規{added}件・取込済のためスキップ{skipped}件"
    repo.record_import(store, user["id"], kind, file.filename or "", added, skipped + parsed.skipped, msg)
    flash(request, f"{file.filename}: {msg}（対象外の行 {parsed.skipped}件）", "success")
    for w in parsed.warnings[:20]:
        flash(request, f"{file.filename} {w}", "warn")
