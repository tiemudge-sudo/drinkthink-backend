"""Endpoint-level regression tests for v3.2 ingredient continuity."""

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
        self.rows = rows

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


class UpdateResult:
    def __init__(self, matched_count=0, modified_count=0, upserted_id=None):
        self.matched_count = matched_count
        self.modified_count = modified_count
        self.upserted_id = upserted_id


class Collection:
    def __init__(self, rows=()):
        self.rows = list(copy.deepcopy(rows))

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
        return Cursor([row for row in self.rows if self._matches(row, query)])

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
        self.location_drinks = Collection()
        self.blocked = Collection()
        self.favorites = Collection()


async def _glasses():
    return {"rocks": {"glass_id": "rocks", "display_name": "Rocks", "icon_key": "rocks", "filter_families": ["rocks"]}}


def configure(monkeypatch):
    database = Database()
    monkeypatch.setattr(server, "db", database)
    monkeypatch.setattr(server, "_canonical_glasses_by_id", _glasses)

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
