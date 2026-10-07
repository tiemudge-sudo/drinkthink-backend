"""Endpoint-level regression tests for v3.2 ingredient continuity."""

import asyncio
import copy
import os

import pytest
from fastapi import HTTPException, Response

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "drinkthink_test")

import server


def test_canonical_read_model_ttl_defaults_to_24_hours_and_honors_bounded_override(monkeypatch):
    monkeypatch.delenv("CANONICAL_READ_MODEL_CACHE_TTL_SECONDS", raising=False)
    assert server._canonical_read_model_ttl_seconds() == 86400.0

    monkeypatch.setenv("CANONICAL_READ_MODEL_CACHE_TTL_SECONDS", "120")
    assert server._canonical_read_model_ttl_seconds() == 120.0


@pytest.mark.parametrize(
    ("configured_value", "expected"),
    [
        ("0", 1.0),
        ("-1", 1.0),
        ("999999", 86400.0),
        ("not-a-number", 86400.0),
    ],
)
def test_canonical_read_model_ttl_rejects_unsafe_values(monkeypatch, configured_value, expected):
    monkeypatch.setenv("CANONICAL_READ_MODEL_CACHE_TTL_SECONDS", configured_value)
    assert server._canonical_read_model_ttl_seconds() == expected


class Cursor:
    def __init__(self, rows, collection=None):
        self.rows = rows
        self.collection = collection

    async def to_list(self, length=None):
        if self.collection and self.collection.fail_next_to_list:
            self.collection.fail_next_to_list = False
            raise RuntimeError("simulated collection read failure")
        return copy.deepcopy(self.rows if length is None else self.rows[:length])

    def __aiter__(self):
        self._iterator = iter(copy.deepcopy(self.rows))
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as error:
            raise StopAsyncIteration from error


class UpdateResult:
    def __init__(self, matched_count=0, modified_count=0, upserted_id=None):
        self.matched_count = matched_count
        self.modified_count = modified_count
        self.upserted_id = upserted_id


class Collection:
    def __init__(self, rows=()):
        self.rows = list(copy.deepcopy(rows))
        self.find_calls = 0
        self.fail_next_to_list = False

    @staticmethod
    def _matches(row, query):
        for field, expected in query.items():
            actual = row.get(field)
            if isinstance(expected, dict) and "$in" in expected:
                allowed = expected["$in"]
                if isinstance(actual, list):
                    if not any(value in allowed for value in actual):
                        return False
                elif actual not in allowed:
                    return False
            elif actual != expected:
                return False
        return True

    def find(self, query, _projection=None):
        self.find_calls += 1
        return Cursor([row for row in self.rows if self._matches(row, query)], self)

    async def find_one(self, query, _projection=None):
        return next((copy.deepcopy(row) for row in self.rows if self._matches(row, query)), None)

    async def update_one(self, query, update, upsert=False):
        for row in self.rows:
            if self._matches(row, query):
                before = copy.deepcopy(row)
                row.update(copy.deepcopy(update.get("$set", {})))
                return UpdateResult(1, int(row != before))
        if upsert:
            created = {key: value for key, value in query.items() if not isinstance(value, dict)}
            created.update(copy.deepcopy(update.get("$set", {})))
            self.rows.append(created)
            return UpdateResult(0, 0, "created")
        return UpdateResult()


class Database:
    def __init__(self):
        self.ingredient_id_merges = Collection([
            {"from_id": 1266, "to_id": 1261, "resolved_to_id": 1261, "status": "active"},
            {"from_id": 1269, "to_id": 1261, "resolved_to_id": 1261, "status": "active"},
            {"from_id": 2403, "to_id": 1647, "resolved_to_id": 1647, "status": "active"},
        ])
        self.ingredients = Collection([
            {"ingredient_id": 1261, "status": "active", "primary_ingredient": "vodka", "primary_category": "spirit"},
            {"ingredient_id": 1647, "status": "active", "primary_ingredient": "rum", "primary_category": "spirit"},
        ])
        self.glasses = Collection([
            {"glass_id": "rocks", "display_name": "Rocks", "filter_families": ["rocks"], "status": "active"},
        ])
        scores = {"fancy": 5, "strong": 5, "thirsty": 5, "comfort": 5, "party": 5}
        self.cocktails = Collection([
            {"cocktail_id": "canonical-cupboard", "name": "Canonical", "status": "active", "glass_id": "rocks", "main_ingredient_ids": [1261], "scores": scores},
            {"cocktail_id": "retired-recipe", "name": "Retired recipe", "status": "active", "glass_id": "rocks", "main_ingredient_ids": [1269], "scores": scores},
        ])
        self.cocktail_ingredients = Collection([
            {"cocktail_id": "canonical-cupboard", "ingredient_id": 1261, "required": True},
            {"cocktail_id": "retired-recipe", "ingredient_id": 1269, "required": True},
        ])
        self.user_cupboard = Collection()
        self.location_settings = Collection()
        self.location_inventory = Collection()
        self.location_drinks = Collection()
        self.blocked = Collection()
        self.favorites = Collection()


async def _glasses():
    return {"rocks": {"glass_id": "rocks", "display_name": "Rocks", "filter_families": ["rocks"]}}


def configure(monkeypatch):
    database = Database()
    monkeypatch.setattr(server, "db", database)
    server._canonical_read_model_cache.clear()

    async def current_user(_authorization):
        return server.User(user_id="user-1", email="user@example.test", name="User", picture="", created_at="now")

    monkeypatch.setattr(server, "current_user", current_user)
    return database


def save(database, item_ids):
    return asyncio.run(server.save_cupboard(server.CupboardRequest(item_ids=item_ids, active=True), server.User(
        user_id="user-1", email="user@example.test", name="User", picture="", created_at="now"
    )))


def read():
    return asyncio.run(server.get_cupboard(server.User(
        user_id="user-1", email="user@example.test", name="User", picture="", created_at="now"
    )))


def match():
    return asyncio.run(server.match_drink(
        strong=5, fancy=5, comfort=5, party=5, thirsty=5, limit=50, authorization="Bearer test"
    ))


def test_cupboard_write_keeps_canonical_id_and_resolves_retired_ids(monkeypatch):
    database = configure(monkeypatch)
    assert save(database, ["1261"]) == {"ok": True, "item_ids": ["1261"], "active": True}
    assert database.user_cupboard.rows[0]["item_ids"] == [1261]

    assert save(database, ["1266", "1261", "1269"]) == {
        "ok": True, "item_ids": ["1261"], "active": True
    }
    assert database.user_cupboard.rows[0]["item_ids"] == [1261]


def test_cupboard_write_rejects_unknown_id(monkeypatch):
    database = configure(monkeypatch)
    with pytest.raises(HTTPException, match="unknown or inactive"):
        save(database, ["999999"])
    assert database.user_cupboard.rows == []


def test_cupboard_reads_resolve_historical_values_without_writing(monkeypatch):
    database = configure(monkeypatch)
    database.user_cupboard.rows[:] = [{"user_id": "user-1", "active": True, "item_ids": [1261, 1266, 999999, "bad"]}]
    before = copy.deepcopy(database.user_cupboard.rows)
    assert read() == {"item_ids": ["1261"], "active": True}
    assert database.user_cupboard.rows == before


def test_match_accepts_canonical_and_historical_cupboard_or_recipe_ids(monkeypatch):
    database = configure(monkeypatch)
    database.user_cupboard.rows[:] = [{"user_id": "user-1", "active": True, "item_ids": [1261]}]
    assert {result.drink.id for result in match().results} == {"canonical-cupboard", "retired-recipe"}

    database.user_cupboard.rows[:] = [{"user_id": "user-1", "active": True, "item_ids": [1266]}]
    assert {result.drink.id for result in match().results} == {"canonical-cupboard", "retired-recipe"}


def test_main_ingredient_retired_and_canonical_ids_are_equivalent(monkeypatch):
    configure(monkeypatch)
    result = asyncio.run(server.match_drink(
        strong=5, fancy=5, comfort=5, party=5, thirsty=5, alcohols="vodka", limit=50, authorization=None
    ))
    assert {item.drink.id for item in result.results} == {"canonical-cupboard", "retired-recipe"}


def test_match_reuses_only_canonical_read_model_and_preserves_order(monkeypatch):
    """A warm cache must not change recommendation results or re-read catalog sources."""
    database = configure(monkeypatch)
    cold_response = Response()
    cold = asyncio.run(server.match_drink(
        strong=5, fancy=5, comfort=5, party=5, thirsty=5, limit=50, response=cold_response
    ))
    warm_response = Response()
    warm = asyncio.run(server.match_drink(
        strong=5, fancy=5, comfort=5, party=5, thirsty=5, limit=50, response=warm_response
    ))

    assert [(item.drink.id, item.score) for item in warm.results] == [
        (item.drink.id, item.score) for item in cold.results
    ]
    assert database.cocktails.find_calls == 1
    assert database.ingredients.find_calls == 1
    assert database.glasses.find_calls == 1
    assert database.ingredient_id_merges.find_calls == 1
    assert 'cache;desc="refresh"' in cold_response.headers["server-timing"]
    assert 'cache;desc="hit"' in warm_response.headers["server-timing"]


def test_expired_canonical_snapshot_refreshes_from_the_database(monkeypatch):
    database = configure(monkeypatch)
    asyncio.run(server.match_drink(strong=5, fancy=5, comfort=5, party=5, thirsty=5, limit=50))
    database.cocktails.rows.append({
        "cocktail_id": "fresh-after-expiry", "name": "Fresh", "status": "active",
        "glass_id": "rocks", "main_ingredient_ids": [1261],
        "scores": {"fancy": 5, "strong": 5, "thirsty": 5, "comfort": 5, "party": 5},
    })
    server._canonical_read_model_cache._expires_at = 0

    refreshed = asyncio.run(server.match_drink(
        strong=5, fancy=5, comfort=5, party=5, thirsty=5, limit=50
    ))

    assert "fresh-after-expiry" in {item.drink.id for item in refreshed.results}
    assert database.cocktails.find_calls == 2


def test_inactive_cupboard_preserves_the_normal_result_order(monkeypatch):
    database = configure(monkeypatch)
    baseline = match()
    database.user_cupboard.rows[:] = [{"user_id": "user-1", "active": False, "item_ids": [1261]}]
    inactive = match()
    assert [(item.drink.id, item.score) for item in inactive.results] == [
        (item.drink.id, item.score) for item in baseline.results
    ]


def test_active_cupboard_reuses_cached_recipe_requirements_without_a_second_read(monkeypatch):
    database = configure(monkeypatch)
    database.user_cupboard.rows[:] = [{"user_id": "user-1", "active": True, "item_ids": [1261]}]
    cold_response = Response()
    cold = asyncio.run(server.match_drink(
        strong=5, fancy=5, comfort=5, party=5, thirsty=5, limit=50,
        authorization="Bearer test", response=cold_response,
    ))
    warm_response = Response()
    warm = asyncio.run(server.match_drink(
        strong=5, fancy=5, comfort=5, party=5, thirsty=5, limit=50,
        authorization="Bearer test", response=warm_response,
    ))

    assert [(item.drink.id, item.score) for item in warm.results] == [
        (item.drink.id, item.score) for item in cold.results
    ]
    assert database.cocktail_ingredients.find_calls == 1
    timing = warm_response.headers["server-timing"]
    assert "cupboard_lookup" in timing
    assert "recipe_requirement_acquisition" in timing
    assert "cupboard_matching" in timing
    assert "blocked_favorites" in timing


def test_cache_refresh_rebuilds_recipe_requirements(monkeypatch):
    database = configure(monkeypatch)
    database.user_cupboard.rows[:] = [{"user_id": "user-1", "active": True, "item_ids": [1261]}]
    first = match()
    assert {item.drink.id for item in first.results} == {"canonical-cupboard", "retired-recipe"}

    database.cocktail_ingredients.rows[:] = [
        {"cocktail_id": "canonical-cupboard", "ingredient_id": 1647, "required": True},
        {"cocktail_id": "retired-recipe", "ingredient_id": 1647, "required": True},
    ]
    server._canonical_read_model_cache._expires_at = 0
    refreshed = match()

    assert refreshed.results == []
    assert database.cocktail_ingredients.find_calls == 2


def test_invalid_cached_relationship_remains_ineligible(monkeypatch):
    database = configure(monkeypatch)
    database.cocktail_ingredients.rows[:] = [
        {"cocktail_id": "canonical-cupboard", "ingredient_id": 1261, "required": True},
        {"cocktail_id": "canonical-cupboard", "ingredient_id": "not-an-id", "required": True},
        {"cocktail_id": "retired-recipe", "ingredient_id": 1269, "required": True},
    ]
    database.user_cupboard.rows[:] = [{"user_id": "user-1", "active": True, "item_ids": [1261]}]
    result = match()
    assert {item.drink.id for item in result.results} == {"retired-recipe"}


def test_checked_in_location_still_replaces_active_cupboard(monkeypatch):
    database = configure(monkeypatch)
    database.user_cupboard.rows[:] = [{"user_id": "user-1", "active": True, "item_ids": [1261]}]
    database.location_drinks.rows[:] = [
        {"location_id": "loc-one", "cocktail_id": "retired-recipe", "can_make": True},
    ]
    result = asyncio.run(server.match_drink(
        strong=5, fancy=5, comfort=5, party=5, thirsty=5, limit=50,
        location_id="loc-one", authorization="Bearer test",
    ))
    assert {item.drink.id for item in result.results} == {"retired-recipe"}


def test_failed_cache_refresh_keeps_the_previous_complete_snapshot(monkeypatch):
    database = configure(monkeypatch)
    database.user_cupboard.rows[:] = [{"user_id": "user-1", "active": True, "item_ids": [1261]}]
    first = match()
    snapshot_before_failure = server._canonical_read_model_cache._snapshot
    database.cocktail_ingredients.fail_next_to_list = True
    server._canonical_read_model_cache._expires_at = 0

    with pytest.raises(RuntimeError, match="simulated collection read failure"):
        match()

    assert server._canonical_read_model_cache._snapshot is snapshot_before_failure
    recovered = match()
    assert [(item.drink.id, item.score) for item in recovered.results] == [
        (item.drink.id, item.score) for item in first.results
    ]
