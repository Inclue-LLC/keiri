"""データの保存先。

ドキュメント（dict）をコレクションごとに保存する最小限のインターフェースで、
ローカル用の FileStore（JSON ファイル）と本番用の FirestoreStore を切り替えて使う。
一意性（同じ請求書番号・同じ明細を二重に登録しない等）はドキュメント ID で担保する。
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable


class Conflict(Exception):
    """create で同じ ID のドキュメントが既にあった。"""


class Txn:
    """トランザクション内の操作。読み取りはすべて書き込みより前に行うこと（Firestore の制約）。"""

    def get(self, coll: str, doc_id: str) -> dict | None: ...
    def set(self, coll: str, doc_id: str, data: dict) -> None: ...
    def create(self, coll: str, doc_id: str, data: dict) -> None: ...
    def update(self, coll: str, doc_id: str, fields: dict) -> None: ...
    def delete(self, coll: str, doc_id: str) -> None: ...


class Store:
    def get(self, coll: str, doc_id: str) -> dict | None:
        raise NotImplementedError

    def get_many(self, coll: str, ids: list[str]) -> dict[str, dict]:
        raise NotImplementedError

    def all(self, coll: str) -> list[dict]:
        raise NotImplementedError

    def set(self, coll: str, doc_id: str, data: dict) -> None:
        raise NotImplementedError

    def create(self, coll: str, doc_id: str, data: dict) -> bool:
        """新規作成。既に存在すれば何もせず False。"""
        raise NotImplementedError

    def update(self, coll: str, doc_id: str, fields: dict) -> None:
        raise NotImplementedError

    def delete(self, coll: str, doc_id: str) -> None:
        raise NotImplementedError

    def set_many(self, coll: str, docs: dict[str, dict]) -> None:
        raise NotImplementedError

    def run_transaction(self, fn: Callable[[Txn], Any]) -> Any:
        raise NotImplementedError


def _with_id(doc_id: str, data: dict) -> dict:
    return {**data, "id": doc_id}


# ---------------------------------------------------------------- ローカル（JSON ファイル）

class FileStore(Store):
    """メモリ上に保持し、書き込みのたびに JSON ファイルへ保存する。path が None ならメモリのみ（テスト用）。"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, dict]] = {}
        if self.path and self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))

    def _flush(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def _coll(self, coll: str) -> dict[str, dict]:
        return self._data.setdefault(coll, {})

    def get(self, coll, doc_id):
        with self._lock:
            doc = self._coll(coll).get(doc_id)
            return copy.deepcopy(doc) if doc is not None else None

    def get_many(self, coll, ids):
        with self._lock:
            c = self._coll(coll)
            return {i: copy.deepcopy(c[i]) for i in ids if i in c}

    def all(self, coll):
        with self._lock:
            return copy.deepcopy(list(self._coll(coll).values()))

    def set(self, coll, doc_id, data):
        with self._lock:
            self._coll(coll)[doc_id] = _with_id(doc_id, copy.deepcopy(data))
            self._flush()

    def create(self, coll, doc_id, data):
        with self._lock:
            if doc_id in self._coll(coll):
                return False
            self.set(coll, doc_id, data)
            return True

    def update(self, coll, doc_id, fields):
        with self._lock:
            c = self._coll(coll)
            if doc_id not in c:
                raise KeyError(doc_id)
            c[doc_id].update(copy.deepcopy(fields))
            self._flush()

    def delete(self, coll, doc_id):
        with self._lock:
            self._coll(coll).pop(doc_id, None)
            self._flush()

    def set_many(self, coll, docs):
        with self._lock:
            c = self._coll(coll)
            for doc_id, data in docs.items():
                c[doc_id] = _with_id(doc_id, copy.deepcopy(data))
            self._flush()

    def run_transaction(self, fn):
        with self._lock:
            txn = _FileTxn(self)
            result = fn(txn)
            txn.commit()
            return result


class _FileTxn(Txn):
    def __init__(self, store: FileStore):
        self.store = store
        self.ops: list[tuple] = []

    def get(self, coll, doc_id):
        return self.store.get(coll, doc_id)

    def set(self, coll, doc_id, data):
        self.ops.append(("set", coll, doc_id, data))

    def create(self, coll, doc_id, data):
        self.ops.append(("create", coll, doc_id, data))

    def update(self, coll, doc_id, fields):
        self.ops.append(("update", coll, doc_id, fields))

    def delete(self, coll, doc_id):
        self.ops.append(("delete", coll, doc_id, None))

    def commit(self):
        s = self.store
        for op, coll, doc_id, _ in self.ops:
            if op == "create" and doc_id in s._coll(coll):
                raise Conflict(f"{coll}/{doc_id}")
        for op, coll, doc_id, data in self.ops:
            c = s._coll(coll)
            if op in ("set", "create"):
                c[doc_id] = _with_id(doc_id, copy.deepcopy(data))
            elif op == "update":
                c[doc_id].update(copy.deepcopy(data))
            else:
                c.pop(doc_id, None)
        if self.ops:
            s._flush()


# ---------------------------------------------------------------- 本番（Firestore）

class FirestoreStore(Store):
    """Firestore に保存する。

    画面表示のたびにコレクション全体を読むため、all() の結果をインスタンス内で短時間キャッシュする。
    このインスタンスからの書き込みでキャッシュは即座に破棄される（Cloud Run は最大インスタンス数 1 を推奨）。
    """

    def __init__(self, client=None, cache_seconds: float = 30):
        from google.cloud import firestore

        self._fs = firestore
        self.client = client or firestore.Client()
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._lock = threading.Lock()

    def _ref(self, coll, doc_id):
        return self.client.collection(coll).document(doc_id)

    def _invalidate(self, *colls: str) -> None:
        with self._lock:
            for c in colls:
                self._cache.pop(c, None)

    def get(self, coll, doc_id):
        snap = self._ref(coll, doc_id).get()
        return _with_id(snap.id, snap.to_dict()) if snap.exists else None

    def get_many(self, coll, ids):
        if not ids:
            return {}
        snaps = self.client.get_all([self._ref(coll, i) for i in ids])
        return {s.id: _with_id(s.id, s.to_dict()) for s in snaps if s.exists}

    def all(self, coll):
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(coll)
            if hit and now - hit[0] < self.cache_seconds:
                return copy.deepcopy(hit[1])
        docs = [_with_id(s.id, s.to_dict()) for s in self.client.collection(coll).stream()]
        with self._lock:
            self._cache[coll] = (now, docs)
        return copy.deepcopy(docs)

    def set(self, coll, doc_id, data):
        self._ref(coll, doc_id).set(_with_id(doc_id, data))
        self._invalidate(coll)

    def create(self, coll, doc_id, data):
        from google.api_core.exceptions import AlreadyExists

        try:
            self._ref(coll, doc_id).create(_with_id(doc_id, data))
        except AlreadyExists:
            return False
        finally:
            self._invalidate(coll)
        return True

    def update(self, coll, doc_id, fields):
        self._ref(coll, doc_id).update(fields)
        self._invalidate(coll)

    def delete(self, coll, doc_id):
        self._ref(coll, doc_id).delete()
        self._invalidate(coll)

    def set_many(self, coll, docs):
        items = list(docs.items())
        for start in range(0, len(items), 400):  # 1バッチの上限は500件
            batch = self.client.batch()
            for doc_id, data in items[start:start + 400]:
                batch.set(self._ref(coll, doc_id), _with_id(doc_id, data))
            batch.commit()
        self._invalidate(coll)

    def run_transaction(self, fn):
        from google.api_core.exceptions import AlreadyExists

        touched: set[str] = set()

        @self._fs.transactional
        def _run(transaction):
            txn = _FirestoreTxn(self, transaction, touched)
            return fn(txn)

        try:
            return _run(self.client.transaction())
        except AlreadyExists as e:
            raise Conflict(str(e)) from e
        finally:
            self._invalidate(*touched)


class _FirestoreTxn(Txn):
    def __init__(self, store: FirestoreStore, transaction, touched: set[str]):
        self.store = store
        self.t = transaction
        self.touched = touched

    def get(self, coll, doc_id):
        snap = self.store._ref(coll, doc_id).get(transaction=self.t)
        return _with_id(snap.id, snap.to_dict()) if snap.exists else None

    def set(self, coll, doc_id, data):
        self.touched.add(coll)
        self.t.set(self.store._ref(coll, doc_id), _with_id(doc_id, data))

    def create(self, coll, doc_id, data):
        self.touched.add(coll)
        self.t.create(self.store._ref(coll, doc_id), _with_id(doc_id, data))

    def update(self, coll, doc_id, fields):
        self.touched.add(coll)
        self.t.update(self.store._ref(coll, doc_id), fields)

    def delete(self, coll, doc_id):
        self.touched.add(coll)
        self.t.delete(self.store._ref(coll, doc_id))
