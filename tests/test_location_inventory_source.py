"""Regression coverage for configured, cupboard-backed location inventory."""

import asyncio
import copy
import os

import pytest
from fastapi import HTTPException


os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "drinkthink_test")

import server


class Cursor:
    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)

    async def to_list(self, length=None):
        return copy.deepcopy(self.rows if length is None else self.rows[:length])

    def __aiter__(self):
        self._iterator = iter(copy.deepcopy(self.rows))
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as error:
            raise StopAsyncIteration from error


def _value_at(row, dotted_name):
    value = row
    for part in dotted_name.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


class Collection:
    def __init__(self, rows=()):
        self.rows = copy.deepcopy(list(rows))

    @staticmethod
    def _matches(row, query):
        for name, expected in query.items():
            actual = _value_at(row, name)
            if isinstance(expected, dict) and "$in" in expected:
                if isinstance(actual, list):
                    if not any(value in expected["$in"] for value in actual):
                        return False
                elif actual not in expected["$in"]:
                    return False
            elif actual != expected:
                return False
        return True

    def find(self, query, _projection=None):
        return Cursor([row for row in self.rows if self._matches(row, query)])

    async def find_one(self, query, _projection=None):
        return next((copy.deepcopy(row) for row in self.rows if self._matches(row, query)), None)

    async def update_one(self, query, update, upsert=False):
        for row in self.rows:
            if self._matches(row, query):
                row.update(copy.deepcopy(update.get("$set", {})))
                return
        if upsert:
            created = {name: value for name, value in query.items() if not isinstance(value, dict)}
            created.update(copy.deepcopy(update.get("$set", {})))
            self.rows.append(created)

    async def delete_many(self, query):
        self.rows[:] = [row for row in self.rows if not self._matches(row, query)]

    async def insert_many(self, rows):
        self.rows.extend(copy.deepcopy(rows))


class Database:
    def __init__(self):
        scores = {"fancy": 5, "strong": 5, "thirsty": 5, "comfort": 5, "party": 5}
        self.ingredients = Collection([
            {"ingredient_id": 1, "status": "active", "primary_category": "spirit", "primary_ingredient": "vodka"},
            {"ingredient_id": 2, "status": "active", "primary_category": "mixer", "primary_ingredient": "lime"},
        ])
        self.ingredient_id_merges = Collection([
            {"from_id": 1001, "resolved_to_id": 1, "status": "active"},
        ])
        self.glasses = Collection([
            {"glass_id": "rocks", "status": "active", "display_name": "Rocks", "filter_families": ["rocks"]},
        ])
        self.cocktails = Collection([
            {"cocktail_id": "vodka-drink", "status": "active", "name": "Vodka drink", "glass_id": "rocks", "scores": scores},
            {"cocktail_id": "lime-drink", "status": "active", "name": "Lime drink", "glass_id": "rocks", "scores": scores},
            {"cocktail_id": "stale-drink", "status": "active", "name": "Stale drink", "glass_id": "rocks", "scores": scores},
        ])
        self.cocktail_ingredients = Collection([
            {"cocktail_id": "vodka-drink", "ingredient_id": 1, "required": True},
            {"cocktail_id": "lime-drink", "ingredient_id": 2, "required": True},
            {"cocktail_id": "stale-drink", "ingredient_id": "not-an-id", "required": True},
        ])
        self.locations = Collection([
            {"location_id": "loc-cupboard", "status": "active"},
        ])
        self.location_settings = Collection([
            {"location_id": "loc-cupboard", "inventory_source": {"kind": "user_cupboard", "user_id": "host", "require_active": True}},
        ])
        self.user_cupboard = Collection([
            {"user_id": "host", "active": True, "item_ids": [1001, 1, 1]},
            {"user_id": "visitor", "active": True, "item_ids": [2]},
        ])
        self.location_inventory = Collection()
        self.location_drinks = Collection()
        self.blocked = Collection()
        self.favorites = Collection()


def configure(monkeypatch):
    database = Database()
    monkeypatch.setattr(server, "db", database)
    server._canonical_read_model_cache.clear()

    async def visitor(_authorization):
        return server.User(user_id="visitor", email="visitor@example.test", name="Visitor", picture="", created_at="now")

    monkeypatch.setattr(server, "current_user", visitor)
    return database


def _rows_by_cocktail(database):
    return {row["cocktail_id"]: row for row in database.location_drinks.rows}


def test_cupboard_source_is_canonicalized_and_deduplicated(monkeypatch):
    configure(monkeypatch)
    model, _ = asyncio.run(server._canonical_read_model_cache.get(server.RequestTiming()))
    assert asyncio.run(server.resolve_effective_location_inventory("loc-cupboard", model)) == {1}


def test_rebuild_uses_host_not_visitor_and_preserves_recipe_semantics(monkeypatch):
    database = configure(monkeypatch)
    asyncio.run(server.rebuild_location_drinks("loc-cupboard"))
    rows = _rows_by_cocktail(database)
    assert rows["vodka-drink"]["can_make"] is True
    assert rows["lime-drink"]["can_make"] is False
    assert rows["lime-drink"]["missing_ingredient_ids"] == [2]
    assert rows["stale-drink"]["can_make"] is False
    assert rows["stale-drink"]["has_unresolved_requirement"] is True
    assert "orderable" not in rows["vodka-drink"]
    assert "pos_connection_id" not in rows["vodka-drink"]

    response = asyncio.run(server.match_drink(
        strong=5, fancy=5, comfort=5, party=5, thirsty=5, limit=50,
        location_id="loc-cupboard", authorization="Bearer visitor",
    ))
    assert [item.drink.id for item in response.results] == ["vodka-drink"]


def test_inactive_required_host_cupboard_has_no_available_inventory(monkeypatch):
    database = configure(monkeypatch)
    database.user_cupboard.rows[0]["active"] = False
    asyncio.run(server.rebuild_location_drinks("loc-cupboard"))
    assert all(row["can_make"] is False for row in database.location_drinks.rows)


def test_host_cupboard_save_refreshes_only_configured_location(monkeypatch):
    database = configure(monkeypatch)
    asyncio.run(server.rebuild_location_drinks("loc-cupboard"))
    assert _rows_by_cocktail(database)["vodka-drink"]["can_make"] is True

    host = server.User(user_id="host", email="host@example.test", name="Host", picture="", created_at="now")
    asyncio.run(server.save_cupboard(server.CupboardRequest(item_ids=["2"], active=True), host))
    rows = _rows_by_cocktail(database)
    assert rows["vodka-drink"]["can_make"] is False
    assert rows["lime-drink"]["can_make"] is True


def test_rebuild_endpoint_uses_its_dedicated_bearer_credential(monkeypatch):
    monkeypatch.setenv(server.LOCATION_INVENTORY_REBUILD_API_KEY_ENV, "expected")
    with pytest.raises(HTTPException, match="Missing bearer") as missing:
        server._location_inventory_rebuild_authorized(None)
    assert missing.value.status_code == 401
    with pytest.raises(HTTPException, match="Invalid rebuild") as invalid:
        server._location_inventory_rebuild_authorized("Bearer wrong")
    assert invalid.value.status_code == 403
    assert server._location_inventory_rebuild_authorized("Bearer expected") is None

    route = next(route for route in server.app.routes if route.path == "/api/admin/locations/{location_id}/rebuild-drinks")
    assert route.methods == {"POST"}
    assert route.dependencies[0].dependency is server._location_inventory_rebuild_authorized


def test_rebuild_endpoint_rejects_missing_or_inactive_location(monkeypatch):
    database = configure(monkeypatch)
    with pytest.raises(HTTPException, match="Active location does not exist") as missing:
        asyncio.run(server._rebuild_configured_location_drinks("loc-missing"))
    assert missing.value.status_code == 404

    database.locations.rows[0]["status"] = "inactive"
    with pytest.raises(HTTPException, match="Active location does not exist") as inactive:
        asyncio.run(server._rebuild_configured_location_drinks("loc-cupboard"))
    assert inactive.value.status_code == 404
    assert database.location_drinks.rows == []


def test_rebuild_endpoint_rejects_missing_or_invalid_inventory_configuration(monkeypatch):
    database = configure(monkeypatch)
    database.location_settings.rows[:] = []
    with pytest.raises(HTTPException, match="inventory source is not configured") as missing:
        asyncio.run(server._rebuild_configured_location_drinks("loc-cupboard"))
    assert missing.value.status_code == 409

    database.location_settings.rows[:] = [
        {"location_id": "loc-cupboard", "inventory_source": {"kind": "user_cupboard"}},
    ]
    with pytest.raises(HTTPException, match="inventory source is not configured") as invalid:
        asyncio.run(server._rebuild_configured_location_drinks("loc-cupboard"))
    assert invalid.value.status_code == 409
    assert database.location_drinks.rows == []


def test_rebuild_endpoint_rebuilds_only_the_configured_active_location(monkeypatch):
    database = configure(monkeypatch)
    result = asyncio.run(server._rebuild_configured_location_drinks("loc-cupboard"))
    assert result == {"location_id": "loc-cupboard", "generated_rows": 3}
    assert len(database.location_drinks.rows) == 3

