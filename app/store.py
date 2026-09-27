"""Tiny document store: Firestore in production, in-memory for local dev.

Only the handful of operations the app needs — keeps Firestore details out of routes.
Collections are prefixed (default ``ssa_``) because the Firebase project is shared.
"""

import contextvars
import copy
import json
import threading
import time
import uuid
from typing import Any

from app import config


class MemoryStore:
    def __init__(self):
        self._data: dict[str, dict[str, dict]] = {}
        self._lock = threading.Lock()

    def get(self, col: str, doc_id: str) -> dict | None:
        doc = self._data.get(col, {}).get(doc_id)
        return copy.deepcopy(doc) if doc else None

    def put(self, col: str, doc_id: str, data: dict) -> None:
        with self._lock:
            self._data.setdefault(col, {})[doc_id] = copy.deepcopy(data)

    def update(self, col: str, doc_id: str, fields: dict) -> None:
        with self._lock:
            self._data.setdefault(col, {}).setdefault(doc_id, {}).update(copy.deepcopy(fields))

    def delete(self, col: str, doc_id: str) -> None:
        with self._lock:
            self._data.get(col, {}).pop(doc_id, None)

    def list(self, col: str, **where: Any) -> list[dict]:
        docs = self._data.get(col, {}).values()
        return [copy.deepcopy(d) for d in docs if all(d.get(k) == v for k, v in where.items())]


class FirestoreStore:
    def __init__(self):
        import firebase_admin
        from firebase_admin import credentials, firestore

        if config.FIREBASE_CREDENTIALS_JSON:
            cred = credentials.Certificate(json.loads(config.FIREBASE_CREDENTIALS_JSON))
        else:
            cred = credentials.Certificate(config.FIREBASE_CREDENTIALS_PATH)
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        self._db = firestore.client()
        self._filter = firestore.FieldFilter

    def _col(self, col: str):
        return self._db.collection(config.COLLECTION_PREFIX + col)

    def get(self, col, doc_id):
        snap = self._col(col).document(doc_id).get()
        return snap.to_dict() if snap.exists else None

    def put(self, col, doc_id, data):
        self._col(col).document(doc_id).set(data)

    def update(self, col, doc_id, fields):
        self._col(col).document(doc_id).set(fields, merge=True)

    def delete(self, col, doc_id):
        self._col(col).document(doc_id).delete()

    def list(self, col, **where):
        q = self._col(col)
        for k, v in where.items():
            q = q.where(filter=self._filter(k, "==", v))
        return [s.to_dict() for s in q.stream()]


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# ---- instrumentation + cache --------------------------------------------------

# Per-request stats: [backend calls, seconds spent in them]. Set by the timing middleware.
db_stats: contextvars.ContextVar[list | None] = contextvars.ContextVar("db_stats", default=None)


class CachedStore:
    """Read-through cache in front of the real store.

    Every Firestore call is a network round trip (~0.1–0.3 s from India), and a page
    needs several: the signed-in user, market hours, alerts, broker, contacts. Reads are
    served from memory; every write goes through here and drops the affected entries,
    so what you see is always what was last saved. This is safe because the app runs
    as ONE process (required anyway for the scanner). Edits made directly in the
    Firebase console show up after CACHE_TTL_SECONDS.
    """

    def __init__(self, backend, ttl: float):
        self.backend = backend
        self.ttl = ttl
        self._docs: dict[tuple[str, str], tuple[float, dict | None]] = {}
        self._lists: dict[tuple[str, frozenset], tuple[float, list[dict]]] = {}
        self._lock = threading.RLock()

    def _call(self, fn, *args, **kwargs):
        start = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            stats = db_stats.get()
            if stats is not None:
                stats[0] += 1
                stats[1] += time.perf_counter() - start

    def _fresh(self, entry) -> bool:
        return entry is not None and entry[0] > time.monotonic()

    def get(self, col: str, doc_id: str) -> dict | None:
        key = (col, doc_id)
        entry = self._docs.get(key)
        if not self._fresh(entry):
            entry = (time.monotonic() + self.ttl, self._call(self.backend.get, col, doc_id))
            with self._lock:
                self._docs[key] = entry
        return copy.deepcopy(entry[1])

    def list(self, col: str, **where: Any) -> list[dict]:
        key = (col, frozenset(where.items()))
        entry = self._lists.get(key)
        if not self._fresh(entry):
            entry = (time.monotonic() + self.ttl, self._call(self.backend.list, col, **where))
            with self._lock:
                self._lists[key] = entry
        return copy.deepcopy(entry[1])

    def _invalidate(self, col: str, doc_id: str) -> None:
        with self._lock:
            self._docs.pop((col, doc_id), None)
            for key in [k for k in self._lists if k[0] == col]:
                del self._lists[key]

    def put(self, col: str, doc_id: str, data: dict) -> None:
        self._call(self.backend.put, col, doc_id, data)
        self._invalidate(col, doc_id)

    def update(self, col: str, doc_id: str, fields: dict) -> None:
        self._call(self.backend.update, col, doc_id, fields)
        self._invalidate(col, doc_id)

    def delete(self, col: str, doc_id: str) -> None:
        self._call(self.backend.delete, col, doc_id)
        self._invalidate(col, doc_id)

    def warm(self, col: str) -> None:
        """Load a whole (small) collection in one call and cache each document."""
        expires = time.monotonic() + self.ttl
        docs = self.list(col)
        id_field = {"users": "username", "alerts": "id"}.get(col)
        with self._lock:
            for d in docs:
                if id_field and d.get(id_field):
                    self._docs[(col, d[id_field])] = (expires, d)

    def clear(self) -> None:
        with self._lock:
            self._docs.clear()
            self._lists.clear()


if config.STORAGE == "memory":
    _backend = MemoryStore()
    print("[storage] IN-MEMORY: nothing is saved. Data is lost on restart (STORAGE=memory).", flush=True)
else:
    _backend = FirestoreStore()
    print(f"[storage] Firestore project '{_backend._db.project}', collections prefixed '{config.COLLECTION_PREFIX}', "
          f"read cache {config.CACHE_TTL_SECONDS:g}s.", flush=True)

# Memory needs no cache (ttl 0 = always read through), but still gets call counting.
store = CachedStore(_backend, 0 if config.STORAGE == "memory" else config.CACHE_TTL_SECONDS)
