"""Non-production unit tests for transactional account deletion."""
import asyncio
import copy
import os

import pytest
from fastapi import HTTPException
from pymongo.errors import PyMongoError

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "drinkthink_test")

import server


class DeleteResult:
    def __init__(self, deleted_count):
        self.deleted_count = deleted_count


class FakeCollection:
    def __init__(self, documents, fail=False):
        self.documents = documents
        self.fail = fail

    @staticmethod
    def _matches(document, query):
        if "$or" in query:
            return any(FakeCollection._matches(document, clause) for clause in query["$or"])
        return all(document.get(key) == value for key, value in query.items())

    async def delete_many(self, query, session=None):
        if self.fail:
            raise PyMongoError("simulated write failure")
        before = len(self.documents)
        self.documents[:] = [d for d in self.documents if not self._matches(d, query)]
        return DeleteResult(before - len(self.documents))

    async def delete_one(self, query, session=None):
        if self.fail:
            raise PyMongoError("simulated write failure")
        for index, document in enumerate(self.documents):
            if self._matches(document, query):
                self.documents.pop(index)
                return DeleteResult(1)
        return DeleteResult(0)

    async def find_one(self, query, projection=None):
        for document in self.documents:
            if self._matches(document, query):
                return copy.deepcopy(document)
        return None


class FakeDb:
    def __init__(self, failing_collection=None):
        self.users = FakeCollection([
            {"user_id": "user_delete", "email": "delete@example.test", "name": "Delete", "picture": "", "created_at": "now"},
            {"user_id": "user_keep", "email": "keep@example.test", "name": "Keep", "picture": "", "created_at": "now"},
        ], fail=failing_collection == "users")
        self.user_sessions = FakeCollection([
            {"session_token": "delete-current", "user_id": "user_delete"},
            {"session_token": "delete-other", "user_id": "user_delete"},
            {"session_token": "keep-current", "user_id": "user_keep"},
        ], fail=failing_collection == "user_sessions")
        self.favorites = FakeCollection([
            {"user_id": "user_delete", "drink_id": "cocktail_delete"},
            {"user_id": "user_keep", "drink_id": "cocktail_keep"},
        ], fail=failing_collection == "favorites")
        self.blocked = FakeCollection([
            {"user_id": "user_delete", "drink_id": "cocktail_blocked"},
            {"user_id": "user_keep", "drink_id": "cocktail_keep"},
        ], fail=failing_collection == "blocked")
        self.user_cupboard = FakeCollection([
            {"user_id": "user_delete", "item_ids": [1], "active": True},
            {"user_id": "user_keep", "item_ids": [2], "active": True},
        ], fail=failing_collection == "user_cupboard")
        self.pending_shares = FakeCollection([
            {"share_id": "sender", "sender_user_id": "user_delete", "recipient_user_id": "user_keep", "sender_email": "delete@example.test"},
            {"share_id": "recipient", "sender_user_id": "user_keep", "recipient_user_id": "user_delete", "recipient_email": "delete@example.test"},
            {"share_id": "keep", "sender_user_id": "user_keep", "recipient_user_id": "user_keep"},
        ], fail=failing_collection == "pending_shares")
        self.share_checkins = FakeCollection([
            {"share_id": "checkin-delete", "sharer_user_id": "user_delete"},
            {"share_id": "checkin-keep", "sharer_user_id": "user_keep"},
        ], fail=failing_collection == "share_checkins")
        self.premium_entitlements = FakeCollection([
            {"user_id": "user_delete", "platform": "ios", "product_id": "app.drinkthink.premium"},
            {"user_id": "user_keep", "platform": "android", "product_id": "app.drinkthink.premium"},
        ], fail=failing_collection == "premium_entitlements")

    def snapshot(self):
        return {name: copy.deepcopy(getattr(self, name).documents) for name in self.__dict__}

    def restore(self, snapshot):
        for name, documents in snapshot.items():
            getattr(self, name).documents[:] = documents


class FakeTransaction:
    def __init__(self, db):
        self.db = db
        self.snapshot = None

    async def __aenter__(self):
        self.snapshot = self.db.snapshot()
        return self

    async def __aexit__(self, exc_type, _exc, _tb):
        if exc_type:
            self.db.restore(self.snapshot)
        return False


class FakeSession:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False

    def start_transaction(self):
        return FakeTransaction(self.db)


class FakeClient:
    def __init__(self, db):
        self.db = db

    async def start_session(self):
        return FakeSession(self.db)


@pytest.fixture
def backend_state(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(server, "db", db)
    monkeypatch.setattr(server, "client", FakeClient(db))
    return db


def delete_as_current_user():
    return asyncio.run(server.delete_account(server.User(
        user_id="user_delete", email="delete@example.test", name="Delete", picture="", created_at="now"
    )))


def test_authenticated_user_deletes_only_own_data_and_all_sessions(backend_state):
    assert delete_as_current_user() == {"ok": True}
    assert backend_state.users.documents == [{"user_id": "user_keep", "email": "keep@example.test", "name": "Keep", "picture": "", "created_at": "now"}]
    assert all(d["user_id"] == "user_keep" for d in backend_state.user_sessions.documents)
    assert all(d["user_id"] == "user_keep" for d in backend_state.favorites.documents)
    assert all(d["user_id"] == "user_keep" for d in backend_state.blocked.documents)
    assert all(d["user_id"] == "user_keep" for d in backend_state.user_cupboard.documents)
    assert [d["share_id"] for d in backend_state.pending_shares.documents] == ["keep"]
    assert [d["share_id"] for d in backend_state.share_checkins.documents] == ["checkin-keep"]
    assert backend_state.premium_entitlements.documents == [{"user_id": "user_keep", "platform": "android", "product_id": "app.drinkthink.premium"}]


def test_unauthenticated_or_old_session_cannot_authenticate_after_deletion(backend_state):
    with pytest.raises(HTTPException) as missing:
        asyncio.run(server.current_user(None))
    assert missing.value.status_code == 401

    delete_as_current_user()
    with pytest.raises(HTTPException) as old_session:
        asyncio.run(server.current_user("Bearer delete-current"))
    assert old_session.value.status_code == 401
    assert asyncio.run(server.current_user("Bearer keep-current")).user_id == "user_keep"


def test_request_has_no_caller_supplied_user_identifier():
    assert "user_id" not in server.delete_account.__annotations__


def test_failure_aborts_and_never_reports_success(monkeypatch):
    db = FakeDb(failing_collection="favorites")
    monkeypatch.setattr(server, "db", db)
    monkeypatch.setattr(server, "client", FakeClient(db))
    original = db.snapshot()

    with pytest.raises(HTTPException) as error:
        delete_as_current_user()

    assert error.value.status_code == 503
    assert db.snapshot() == original
