"""In-memory Firestore: collections, where/order_by/limit, get/set, atomic batch."""

import copy
import operator
from datetime import UTC, datetime, timedelta

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore

OPS = {">=": operator.ge, "<": operator.lt, "==": operator.eq}


class Snap:
    def __init__(self, path: str, data: dict | None) -> None:
        self.id = path.rsplit("/", 1)[-1]
        self.exists = data is not None
        self._data = data

    def to_dict(self) -> dict:
        return copy.deepcopy(self._data or {})


class Ref:
    def __init__(self, db: "FakeDB", path: str) -> None:
        self.db, self.path = db, path

    def collection(self, name: str) -> "Query":
        return Query(self.db, f"{self.path}/{name}")

    def get(self) -> Snap:
        return Snap(self.path, self.db.store.get(self.path))

    def set(self, data: dict) -> None:
        self.db.store[self.path] = copy.deepcopy(data)


class Query:
    def __init__(
        self,
        db: "FakeDB",
        path: str,
        filters: tuple = (),
        order: str | None = None,
        n: int | None = None,
    ) -> None:
        self.db, self.path, self.filters = db, path, filters
        self.order, self.n = order, n

    def document(self, doc_id: str) -> Ref:
        return Ref(self.db, f"{self.path}/{doc_id}")

    def where(self, *, filter: firestore.FieldFilter) -> "Query":
        return Query(self.db, self.path, (*self.filters, filter), self.order, self.n)

    def order_by(self, field: str, direction: str) -> "Query":
        assert direction == firestore.Query.DESCENDING
        return Query(self.db, self.path, self.filters, field, self.n)

    def limit(self, n: int) -> "Query":
        return Query(self.db, self.path, self.filters, self.order, n)

    def stream(self) -> list[Snap]:
        rows = [
            (path, data)
            for path, data in sorted(self.db.store.items())
            if path.rsplit("/", 1)[0] == self.path
            and all(OPS[f.op_string](data[f.field_path], f.value) for f in self.filters)
        ]
        if self.order:
            # Firestore breaks ties by document name in the same direction.
            rows.sort(key=lambda r: (r[1][self.order], r[0]), reverse=True)
        return [Snap(p, d) for p, d in rows[: self.n]]


class Batch:
    def __init__(self, db: "FakeDB") -> None:
        self.db, self.ops = db, []  # type: list[tuple[str, dict]]

    def create(self, ref: Ref, data: dict) -> None:
        self.ops.append((ref.path, data))

    def commit(self) -> None:
        if any(path in self.db.store for path, _ in self.ops):
            raise AlreadyExists("exists")  # atomic: nothing is written
        self.db.now += timedelta(seconds=1)
        for path, data in self.ops:
            assert data["creado"] is firestore.SERVER_TIMESTAMP
            self.db.store[path] = {**copy.deepcopy(data), "creado": self.db.now}
        self.db.commits += 1


class FakeDB:
    def __init__(self) -> None:
        self.store: dict[str, dict] = {}
        self.commits = 0
        self.now = datetime(2026, 9, 29, 17, tzinfo=UTC)

    def collection(self, name: str) -> Query:
        return Query(self, name)

    def batch(self) -> Batch:
        return Batch(self)
