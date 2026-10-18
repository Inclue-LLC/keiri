import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, db
from app.main import create_app

SAMPLES = Path(__file__).resolve().parent.parent / "sample_data"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def client(db_path):
    with db.transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            ("keiri", auth.hash_password("correct-horse-battery")),
        )
    auth._failures.clear()
    return TestClient(create_app(db_path=db_path, secret_key="test-secret"))


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
