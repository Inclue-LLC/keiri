import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, repo
from app.main import create_app
from app.store import FileStore, FirestoreStore

SAMPLES = Path(__file__).resolve().parent.parent / "sample_data"
EMULATOR_PROJECT = "demo-keiri-test"


@pytest.fixture(params=["file", "firestore"])
def store(request):
    """ローカル（FileStore）と Firestore の両方で同じテストを実行する。

    Firestore は FIRESTORE_EMULATOR_HOST でエミュレーターが指定されている場合のみ。
    """
    if request.param == "file":
        return FileStore()
    host = os.environ.get("FIRESTORE_EMULATOR_HOST")
    if not host:
        pytest.skip("Firestore エミュレーターが起動していません（FIRESTORE_EMULATOR_HOST）")
    import httpx
    from google.cloud import firestore

    httpx.delete(f"http://{host}/emulator/v1/projects/{EMULATOR_PROJECT}/databases/(default)/documents")
    return FirestoreStore(firestore.Client(project=EMULATOR_PROJECT), cache_seconds=0)


@pytest.fixture
def client(store):
    repo.create_user(store, "keiri", auth.hash_password("correct-horse-battery"))
    auth._failures.clear()
    return TestClient(create_app(store=store, secret_key="test-secret"))


def csrf_of(client: TestClient, path: str = "/login") -> str:
    html = client.get(path).text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


@pytest.fixture
def logged_in(client):
    token = csrf_of(client)
    r = client.post(
        "/login",
        data={"username": "keiri", "password": "correct-horse-battery", "csrf": token},
        follow_redirects=False,
    )
    assert r.status_code == 303
    return client


# --- テストデータの登録 ---

def add_client(store, name, kana="", aliases=()):
    cid = repo.client_id_for(name)
    store.set("clients", cid, {"name": name, "name_kana": kana, "ts": 0, "created_at": ""})
    for a in aliases:
        repo.add_alias(store, cid, a)
    return cid


def add_invoice(store, no, client_id, issue, due, amount):
    iid = repo.invoice_id_for(no)
    store.set("invoices", iid, {
        "invoice_no": no, "client_id": client_id, "subject": "", "issue_date": issue, "due_date": due,
        "amount": amount, "memo": "", "ts": 0, "created_at": "",
    })
    return iid


def add_tx(store, date, amount, payer, key=None):
    tid = repo.transaction_id_for(key or f"{date}-{amount}-{payer}")
    store.set("transactions", tid, {
        "bank": "smbc", "date": date, "amount": amount, "description": "", "payer_kana": payer,
        "balance": None, "invoice_ids": [], "ts": 0, "created_at": "",
    })
    return tid
