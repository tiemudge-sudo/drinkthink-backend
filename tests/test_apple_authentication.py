"""Focused unit coverage for native Sign in with Apple.

Apple JWKS retrieval is always mocked; these tests make no Apple network calls.
"""
import asyncio
import copy
import os
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "drinkthink_test")

import server


class FakeCollection:
    def __init__(self, documents=None):
        self.documents = documents or []

    async def find_one(self, query, projection=None):
        for document in self.documents:
            if all(document.get(key) == value for key, value in query.items()):
                return copy.deepcopy(document)
        return None

    async def insert_one(self, document):
        self.documents.append(copy.deepcopy(document))


class FakeDb:
    def __init__(self):
        self.users = FakeCollection()
        self.user_sessions = FakeCollection()


@pytest.fixture
def auth_db(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(server, "db", db)
    return db


def apple_token(private_key, **claims):
    now = datetime.now(timezone.utc)
    payload = {
        "iss": server.APPLE_IDENTITY_TOKEN_ISSUER,
        "aud": server.APPLE_AUTH_AUDIENCE_DEFAULT,
        "sub": "apple-stable-subject",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        **claims,
    }
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": "test-kid"})


def test_identity_token_requires_valid_apple_signature_issuer_audience_and_expiry(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    async def public_key(_kid):
        return private_key.public_key()

    monkeypatch.setattr(server, "_apple_public_key_for_kid", public_key)
    claims = asyncio.run(server._verify_apple_identity_token(apple_token(private_key)))
    assert claims["sub"] == "apple-stable-subject"

    with pytest.raises(HTTPException) as wrong_audience:
        asyncio.run(server._verify_apple_identity_token(apple_token(private_key, aud="other.bundle")))
    assert wrong_audience.value.status_code == 401

    with pytest.raises(HTTPException) as wrong_issuer:
        asyncio.run(server._verify_apple_identity_token(apple_token(private_key, iss="https://issuer.invalid")))
    assert wrong_issuer.value.status_code == 401

    with pytest.raises(HTTPException) as expired:
        asyncio.run(server._verify_apple_identity_token(apple_token(private_key, exp=1)))
    assert expired.value.status_code == 401

    different_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(HTTPException) as invalid_signature:
        asyncio.run(server._verify_apple_identity_token(apple_token(different_key)))
    assert invalid_signature.value.status_code == 401


def test_verified_apple_subject_creates_then_reuses_existing_drinkthink_account(auth_db, monkeypatch):
    async def verified(_token):
        return {
            "sub": "apple-stable-subject",
            "email": "relay@privaterelay.appleid.com",
            "email_verified": "true",
        }

    monkeypatch.setattr(server, "_verify_apple_identity_token", verified)
    request = server.AppleAuthenticationRequest(identity_token="identity-token", full_name="Apple Reviewer")

    first = asyncio.run(server.auth_apple(request))
    second = asyncio.run(server.auth_apple(request))

    assert first.user.user_id == second.user.user_id
    assert first.user.email == "relay@privaterelay.appleid.com"
    assert len(auth_db.users.documents) == 1
    assert auth_db.users.documents[0]["auth_provider"] == "apple"
    assert auth_db.users.documents[0]["auth_subject"] == "apple-stable-subject"
    assert len(auth_db.user_sessions.documents) == 2
    assert first.session_token != second.session_token


def test_unverified_email_cannot_create_apple_account(auth_db, monkeypatch):
    async def unverified(_token):
        return {"sub": "apple-stable-subject", "email": "unverified@example.test", "email_verified": False}

    monkeypatch.setattr(server, "_verify_apple_identity_token", unverified)
    with pytest.raises(HTTPException) as error:
        asyncio.run(server.auth_apple(server.AppleAuthenticationRequest(identity_token="identity-token")))

    assert error.value.status_code == 401
    assert auth_db.users.documents == []
    assert auth_db.user_sessions.documents == []


def test_apple_account_does_not_silently_merge_with_existing_email(auth_db, monkeypatch):
    auth_db.users.documents.append({
        "user_id": "existing-google-user",
        "email": "same@example.test",
        "name": "Google User",
        "picture": "",
        "created_at": "now",
    })

    async def verified(_token):
        return {"sub": "apple-stable-subject", "email": "same@example.test", "email_verified": True}

    monkeypatch.setattr(server, "_verify_apple_identity_token", verified)
    with pytest.raises(HTTPException) as error:
        asyncio.run(server.auth_apple(server.AppleAuthenticationRequest(identity_token="identity-token")))

    assert error.value.status_code == 409
    assert len(auth_db.users.documents) == 1
    assert auth_db.user_sessions.documents == []
