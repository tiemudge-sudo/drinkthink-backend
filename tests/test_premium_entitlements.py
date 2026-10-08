"""Unit tests for Premium store verification. All Apple/Google calls are mocked."""
import asyncio
import base64
import copy
import json
import logging
import os
import pytest
from fastapi import HTTPException
import httpx

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "drinkthink_test")
os.environ.setdefault("PREMIUM_PURCHASE_IDENTITY_HMAC_KEY", "test-hmac-key")
os.environ.setdefault("PREMIUM_VERIFICATION_REFERENCE_ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
import server

class Collection:
    def __init__(self, documents=None): self.documents = documents or []
    @staticmethod
    def matches(doc, query): return all(doc.get(k) == v for k, v in query.items())
    async def find_one(self, query, projection=None): return copy.deepcopy(next((x for x in self.documents if self.matches(x, query)), None))
    async def update_one(self, query, update, upsert=False):
        doc = next((x for x in self.documents if self.matches(x, query)), None)
        if doc is None and upsert:
            doc = dict(query); doc.update(update.get("$setOnInsert", {})); self.documents.append(doc)
        if doc is not None: doc.update(update.get("$set", {}))
    async def delete_many(self, query, session=None): self.documents[:] = [x for x in self.documents if not self.matches(x, query)]

class Db:
    def __init__(self, docs=None): self.premium_entitlements = Collection(docs)

def user(name="account_a"): return server.User(user_id=name, email=f"{name}@example.test", name=name, picture="", created_at="now")
def request(platform="ios", **evidence): return server.PremiumVerificationRequest(platform=platform, product_id=server.PREMIUM_PRODUCT_ID, **evidence)
def run(coro): return asyncio.run(coro)


def apple_jws(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


class AppleResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class AppleClient:
    result = None

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, *_args, **_kwargs):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def configure_apple_lookup(monkeypatch, result):
    """Use a fake HTTP transport without permitting sensitive URL logging."""
    monkeypatch.setenv("APPLE_APP_STORE_PRIVATE_KEY", "PRIVATE_KEY_MUST_NEVER_BE_LOGGED")
    monkeypatch.setenv("APPLE_APP_STORE_ISSUER_ID", "issuer-secret")
    monkeypatch.setenv("APPLE_APP_STORE_KEY_ID", "key-secret")
    monkeypatch.setenv("APPLE_BUNDLE_ID", "com.drinkthink.mobile")
    monkeypatch.setenv("APPLE_APP_STORE_ENVIRONMENT", "Sandbox")
    monkeypatch.setattr(server.jwt, "encode", lambda *_args, **_kwargs: "server-auth-token")
    AppleClient.result = result
    monkeypatch.setattr(server.httpx, "AsyncClient", AppleClient)

@pytest.fixture
def state(monkeypatch):
    db = Db(); monkeypatch.setattr(server, "db", db); return db

def test_free_lookup_active_lookup_and_platform_isolation(state):
    assert not run(server.premium_entitlement("ios", user())).premium
    state.premium_entitlements.documents.append({"user_id":"account_a", "platform":"ios", "product_id":server.PREMIUM_PRODUCT_ID, "status":"active", "last_verified_at":"now"})
    assert run(server.premium_entitlement("ios", user())).premium
    assert not run(server.premium_entitlement("android", user())).premium

def test_valid_apple_verification_is_idempotent(state, monkeypatch):
    async def apple(_): return ("original-1", "transaction-1", "active", False, None)
    monkeypatch.setattr(server, "_verify_apple_purchase", apple)
    assert run(server.verify_premium_purchase(request(transaction_jws="evidence"), user())).premium
    assert run(server.verify_premium_purchase(request(transaction_jws="evidence"), user())).premium
    assert len(state.premium_entitlements.documents) == 1

@pytest.mark.parametrize("message", ["Invalid Apple transaction evidence", "Apple transaction does not match DrinkThink Premium", "Apple transaction could not be verified"])
def test_invalid_apple_evidence_bundle_and_product_rejected(state, monkeypatch, message):
    async def apple(_): raise HTTPException(status_code=400, detail=message)
    monkeypatch.setattr(server, "_verify_apple_purchase", apple)
    with pytest.raises(HTTPException) as error: run(server.verify_premium_purchase(request(transaction_jws="bad"), user()))
    assert error.value.status_code == 400 and not state.premium_entitlements.documents

def test_apple_revocation_returns_free(state, monkeypatch):
    async def apple(_): return ("original-revoked", "transaction", "revoked", True, "refund")
    monkeypatch.setattr(server, "_verify_apple_purchase", apple)
    assert not run(server.verify_premium_purchase(request(transaction_jws="revoked"), user())).premium

def test_valid_google_verification_and_acknowledgement_path(state, monkeypatch):
    async def google(token): assert token == "play-token"; return (token, token, None)
    monkeypatch.setattr(server, "_verify_google_purchase", google)
    assert run(server.verify_premium_purchase(request("android", purchase_token="play-token"), user())).premium
    assert state.premium_entitlements.documents[0]["platform"] == "android"

@pytest.mark.parametrize("message", ["Google Play purchase could not be verified", "Google Play purchase is not valid for DrinkThink Premium", "Google Play verification is unavailable"])
def test_invalid_google_token_package_product_or_state_rejected(state, monkeypatch, message):
    async def google(_): raise HTTPException(status_code=400 if "unavailable" not in message else 503, detail=message)
    monkeypatch.setattr(server, "_verify_google_purchase", google)
    with pytest.raises(HTTPException) as error: run(server.verify_premium_purchase(request("android", purchase_token="bad"), user()))
    assert error.value.detail == message and not state.premium_entitlements.documents

def test_same_purchase_different_existing_account_is_nonidentifying_conflict(state, monkeypatch):
    async def apple(_): return ("original-conflict", "transaction", "active", False, None)
    monkeypatch.setattr(server, "_verify_apple_purchase", apple)
    run(server.verify_premium_purchase(request(transaction_jws="ok"), user("account_a")))
    with pytest.raises(HTTPException) as error: run(server.verify_premium_purchase(request(transaction_jws="ok"), user("account_b")))
    assert error.value.status_code == 409 and "account_a" not in str(error.value.detail)

def test_deleted_association_releases_restore_without_tombstone(state, monkeypatch):
    async def apple(_): return ("original-restore", "transaction", "active", False, None)
    monkeypatch.setattr(server, "_verify_apple_purchase", apple)
    run(server.verify_premium_purchase(request(transaction_jws="ok"), user("old")))
    run(state.premium_entitlements.delete_many({"user_id":"old"}))
    assert run(server.verify_premium_purchase(request(transaction_jws="ok"), user("new"))).premium
    assert state.premium_entitlements.documents[0]["user_id"] == "new"

def test_missing_configuration_fails_safely(monkeypatch):
    monkeypatch.delenv("PREMIUM_PURCHASE_IDENTITY_HMAC_KEY", raising=False)
    with pytest.raises(HTTPException) as error: server._purchase_identity_hash("ios", "x")
    assert error.value.status_code == 503

def test_verification_request_has_no_user_id_field():
    assert "user_id" not in server.PremiumVerificationRequest.model_fields


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError])
def test_apple_lookup_network_errors_are_502_with_redacted_diagnostics(monkeypatch, caplog, error_type):
    configure_apple_lookup(monkeypatch, error_type("https://apple.invalid/transaction-secret-123"))
    evidence = apple_jws({"transactionId": "transaction-secret-123"})

    with caplog.at_level(logging.INFO, logger=server.logger.name):
        with pytest.raises(HTTPException) as error:
            run(server._verify_apple_purchase(evidence))

    assert error.value.status_code == 502
    diagnostic = "\n".join(record.getMessage() for record in caplog.records)
    assert "stage=apple_transaction_lookup" in diagnostic
    assert "environment=sandbox" in diagnostic
    assert f"exception_class={error_type.__name__}" in diagnostic
    for sensitive_value in ("transaction-secret-123", evidence, "PRIVATE_KEY_MUST_NEVER_BE_LOGGED", "issuer-secret", "key-secret"):
        assert sensitive_value not in diagnostic


def test_apple_lookup_non_success_preserves_400_with_safe_diagnostics(monkeypatch, caplog):
    configure_apple_lookup(monkeypatch, AppleResponse(404, {"errorCode": 4040010, "detail": "transaction-secret-123"}))
    evidence = apple_jws({"transactionId": "transaction-secret-123"})

    with caplog.at_level(logging.INFO, logger=server.logger.name):
        with pytest.raises(HTTPException) as error:
            run(server._verify_apple_purchase(evidence))

    assert error.value.status_code == 400
    diagnostic = "\n".join(record.getMessage() for record in caplog.records)
    assert "outcome=upstream_non_success" in diagnostic
    assert "upstream_status=404" in diagnostic
    assert "apple_error_code=4040010" in diagnostic
    assert "transaction-secret-123" not in diagnostic
    assert evidence not in diagnostic


def test_apple_lookup_success_retains_behavior_and_safe_diagnostics(monkeypatch, caplog):
    transaction_id = "transaction-secret-123"
    signed_transaction = apple_jws({
        "bundleId": "com.drinkthink.mobile",
        "productId": server.PREMIUM_PRODUCT_ID,
        "originalTransactionId": "original-secret-456",
    })
    configure_apple_lookup(monkeypatch, AppleResponse(200, {"signedTransactionInfo": signed_transaction}))
    evidence = apple_jws({"transactionId": transaction_id})

    with caplog.at_level(logging.INFO, logger=server.logger.name):
        result = run(server._verify_apple_purchase(evidence))

    assert result == ("original-secret-456", transaction_id, "active", False, None)
    diagnostic = "\n".join(record.getMessage() for record in caplog.records)
    assert "outcome=upstream_response" in diagnostic
    assert "upstream_status=200" in diagnostic
    for sensitive_value in (transaction_id, "original-secret-456", evidence, signed_transaction, "PRIVATE_KEY_MUST_NEVER_BE_LOGGED"):
        assert sensitive_value not in diagnostic


def test_http_client_loggers_do_not_emit_sensitive_request_urls():
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
