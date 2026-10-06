"""Focused tests for the canonical machine-authorized location API."""

import asyncio
import copy
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "drinkthink_test")

import server
from location_geocoding import GeocodingError


ADDRESS = {
    "line1": "100 Main Street", "line2": None, "city": "Tampa",
    "state": "FL", "postal_code": "33601", "country": "US",
}
CHANGED_ADDRESS = {**ADDRESS, "line1": "200 Main Street"}
GEO = {"type": "Point", "coordinates": [-82.5, 27.9]}
CHANGED_GEO = {"type": "Point", "coordinates": [-82.6, 28.0]}


class UpdateResult:
    def __init__(self, upserted_id=None):
        self.upserted_id = upserted_id


class Collection:
    def __init__(self, rows=()):
        self.rows = copy.deepcopy(list(rows))
        self.update_calls = 0

    @staticmethod
    def _matches(row, query):
        return all(row.get(key) == value for key, value in query.items())

    async def find_one(self, query, _projection=None):
        row = next((row for row in self.rows if self._matches(row, query)), None)
        return copy.deepcopy(row)

    async def update_one(self, query, update, upsert=False):
        self.update_calls += 1
        row = next((row for row in self.rows if self._matches(row, query)), None)
        created = row is None
        if created:
            assert upsert
            row = dict(query)
            row.update(copy.deepcopy(update.get("$setOnInsert", {})))
            self.rows.append(row)
        row.update(copy.deepcopy(update.get("$set", {})))
        for field in update.get("$unset", {}):
            row.pop(field, None)
        return UpdateResult("created" if created else None)


class Database:
    def __init__(self, locations=()):
        self.organizations = Collection([{"organization_id": "org_forbici", "name": "Forbici", "status": "active"}])
        self.locations = Collection(locations)
        # These collections make accidental operational side effects visible.
        self.location_inventory = Collection()
        self.location_drinks = Collection()
        self.pos_connections = Collection()
        self.pricing = Collection()


def request(address=ADDRESS, **overrides):
    values = {
        "location_id": "loc_forbici_south_tampa",
        "organization_id": "org_forbici",
        "name": "Forbici",
        "address": address,
        "timezone": "America/New_York",
        "status": "active",
    }
    values.update(overrides)
    return server.LocationProvisioningRequest(**values)


def configure(monkeypatch, locations=(), geocode_results=(GEO,)):
    database = Database(locations)
    calls = []
    results = iter(geocode_results)
    clock = iter([
        datetime(2026, 10, 6, tzinfo=timezone.utc),
        datetime(2026, 10, 6, tzinfo=timezone.utc) + timedelta(seconds=1),
    ])

    async def geocode(address, api_key):
        calls.append(copy.deepcopy(address))
        value = next(results)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    monkeypatch.setattr(server, "db", database)
    monkeypatch.setattr(server, "geocode_canonical_address", geocode)
    monkeypatch.setattr(server, "_now", lambda: next(clock))
    monkeypatch.setenv(server.LOCATION_PROVISIONING_API_KEY_ENV, "test-provisioning-key")
    monkeypatch.setenv("GEOCODIO_API_KEY", "test-geocodio-key")
    return database, calls


def provision(body):
    return asyncio.run(server._provision_canonical_location(body))


def test_provisioning_uses_a_dedicated_bearer_service_credential(monkeypatch):
    monkeypatch.setenv(server.LOCATION_PROVISIONING_API_KEY_ENV, "expected")
    with pytest.raises(HTTPException, match="Missing bearer") as missing:
        server._provisioning_authorized(None)
    assert missing.value.status_code == 401
    with pytest.raises(HTTPException, match="Invalid provisioning") as invalid:
        server._provisioning_authorized("Bearer wrong")
    assert invalid.value.status_code == 403
    assert server._provisioning_authorized("Bearer expected") is None
    monkeypatch.delenv(server.LOCATION_PROVISIONING_API_KEY_ENV)
    with pytest.raises(HTTPException, match="not configured") as unavailable:
        server._provisioning_authorized("Bearer expected")
    assert unavailable.value.status_code == 503


def test_new_location_geocodes_once_and_persists_geojson_in_longitude_latitude_order(monkeypatch):
    database, calls = configure(monkeypatch)
    result = provision(request())

    assert result["created"] is True
    assert result["geocoded"] is True
    assert result["geo"] == GEO
    assert calls == [ADDRESS]
    stored = database.locations.rows[0]
    assert stored["address"] == ADDRESS
    assert stored["geo"] == GEO
    assert stored["pos_connection_id"] is None
    assert not any(key in stored for key in ("latitude", "longitude", "is_test", "check_in_radius_meters"))
    assert not database.location_inventory.rows
    assert not database.location_drinks.rows
    assert not database.pos_connections.rows
    assert not database.pricing.rows


def test_existing_same_address_reuses_geo_and_preserves_created_at(monkeypatch):
    original = {
        "location_id": "loc_forbici_south_tampa", "organization_id": "org_forbici",
        "name": "Old Forbici", "address": ADDRESS, "geo": GEO,
        "timezone": "America/New_York", "status": "active",
        "created_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
    }
    database, calls = configure(monkeypatch, [original])
    result = provision(request(name="Forbici South Tampa"))

    assert result["created"] is False
    assert result["geocoded"] is False
    assert calls == []
    stored = database.locations.rows[0]
    assert stored["created_at"] == original["created_at"]
    assert stored["updated_at"] > original["created_at"]
    assert stored["name"] == "Forbici South Tampa"


def test_changed_address_geocodes_before_atomically_updating_address_and_geo(monkeypatch):
    original = {
        "location_id": "loc_forbici_south_tampa", "organization_id": "org_forbici",
        "name": "Forbici", "address": ADDRESS, "geo": GEO,
        "timezone": "America/New_York", "status": "active",
        "created_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
    }
    database, calls = configure(monkeypatch, [original], [CHANGED_GEO])
    result = provision(request(address=CHANGED_ADDRESS))

    assert result["geocoded"] is True
    assert calls == [CHANGED_ADDRESS]
    assert database.locations.rows[0]["address"] == CHANGED_ADDRESS
    assert database.locations.rows[0]["geo"] == CHANGED_GEO
    assert database.locations.rows[0]["created_at"] == original["created_at"]


def test_failed_changed_address_geocode_preserves_existing_record(monkeypatch):
    original = {
        "location_id": "loc_forbici_south_tampa", "organization_id": "org_forbici",
        "name": "Forbici", "address": ADDRESS, "geo": GEO,
        "timezone": "America/New_York", "status": "active",
        "created_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
    }
    database, calls = configure(monkeypatch, [original], [GeocodingError("no results")])
    before = copy.deepcopy(database.locations.rows)

    with pytest.raises(HTTPException, match="Location geocoding failed") as failure:
        provision(request(address=CHANGED_ADDRESS))

    assert failure.value.status_code == 502
    assert calls == [CHANGED_ADDRESS]
    assert database.locations.rows == before


def test_unknown_organization_and_prohibited_fields_are_rejected(monkeypatch):
    database, calls = configure(monkeypatch)
    unknown = request(organization_id="org_missing")
    with pytest.raises(HTTPException, match="Organization does not exist") as failure:
        provision(unknown)
    assert failure.value.status_code == 404
    assert not calls
    assert not database.locations.rows

    for prohibited in ("latitude", "longitude", "is_test", "check_in_radius_meters", "pos_connection_id"):
        with pytest.raises(ValidationError):
            server.LocationProvisioningRequest(**{**request().model_dump(), prohibited: 1})
    with pytest.raises(ValidationError):
        request(status="pending")


def test_invalid_geocode_does_not_create_a_location(monkeypatch):
    database, _calls = configure(monkeypatch, geocode_results=[GeocodingError("not address-level")])
    with pytest.raises(HTTPException, match="Location geocoding failed"):
        provision(request())
    assert not database.locations.rows
