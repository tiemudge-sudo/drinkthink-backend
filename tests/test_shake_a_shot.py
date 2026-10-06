"""Unit tests for the read-only Shake a Shot selection endpoint."""
import asyncio
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "drinkthink_test")

import server


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length=None):
        return list(self.rows if length is None else self.rows[:length])

    def __aiter__(self):
        self._iterator = iter(self.rows)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as error:
            raise StopAsyncIteration from error


class Collection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, query, _projection=None):
        def matches(row):
            for key, expected in query.items():
                if isinstance(expected, dict) and "$in" in expected:
                    if row.get(key) not in expected["$in"]:
                        return False
                elif row.get(key) != expected:
                    return False
            return True
        return Cursor([row for row in self.rows if matches(row)])

    async def find_one(self, query, _projection=None):
        return next((row for row in self.find(query).rows), None)


class FakeDb:
    def __init__(self):
        self.cocktails = Collection([
            {"cocktail_id": "shot-vodka", "status": "active", "glass_id": "shot", "main_ingredient_ids": [1], "scores": {}},
            {"cocktail_id": "shot-rum", "status": "active", "glass_id": "shot", "main_ingredient_ids": [2], "scores": {}},
            {"cocktail_id": "rocks-vodka", "status": "active", "glass_id": "rocks", "main_ingredient_ids": [1], "scores": {}},
        ])
        self.ingredients = Collection([
            {"ingredient_id": 1, "status": "active", "primary_ingredient": "vodka", "primary_category": "spirit"},
            {"ingredient_id": 2, "status": "active", "primary_ingredient": "rum", "primary_category": "spirit"},
        ])
        self.ingredient_id_merges = Collection([])
        self.location_drinks = Collection([
            {"location_id": "loc-one", "cocktail_id": "shot-rum", "can_make": True},
        ])
        self.user_cupboard = Collection([])
        self.cocktail_ingredients = Collection([])
        self.blocked = Collection([])


async def _glasses():
    return {
        "shot": {"glass_id": "shot", "filter_families": ["shot"], "display_name": "Shot"},
        "rocks": {"glass_id": "rocks", "filter_families": ["rocks"], "display_name": "Rocks"},
    }


def configure(monkeypatch):
    monkeypatch.setattr(server, "db", FakeDb())
    monkeypatch.setattr(server, "_canonical_glasses_by_id", _glasses)


def test_only_canonical_shot_candidates_are_returned_and_repeat_is_avoided(monkeypatch):
    configure(monkeypatch)
    selected = asyncio.run(server.shake_a_shot(previous_drink_id="shot-vodka", authorization=None))
    assert selected.drink.id == "shot-rum"


def test_main_ingredient_and_location_constraints_apply_without_slider_inputs(monkeypatch):
    configure(monkeypatch)
    vodka = asyncio.run(server.shake_a_shot(alcohols="vodka", authorization=None))
    assert vodka.drink.id == "shot-vodka"

    location = asyncio.run(server.shake_a_shot(location_id="loc-one", authorization=None))
    assert location.drink.id == "shot-rum"
    # The endpoint intentionally has no Drink Style or slider inputs.
    assert "glasses" not in server.shake_a_shot.__annotations__
    assert "strong" not in server.shake_a_shot.__annotations__


def test_cupboard_and_blocked_drinks_apply_for_an_authenticated_request(monkeypatch):
    configure(monkeypatch)
    database = server.db
    database.user_cupboard.rows[:] = [{"user_id": "user-1", "active": True, "item_ids": [1]}]
    database.cocktail_ingredients.rows[:] = [
        {"cocktail_id": "shot-vodka", "ingredient_id": 1, "required": True},
        {"cocktail_id": "shot-rum", "ingredient_id": 2, "required": True},
    ]

    async def user(_authorization):
        return server.User(user_id="user-1", email="test@example.test", name="Test", picture="", created_at="now")

    monkeypatch.setattr(server, "current_user", user)
    cupboard = asyncio.run(server.shake_a_shot(use_cupboard=True, authorization="Bearer session"))
    assert cupboard.drink.id == "shot-vodka"

    database.blocked.rows[:] = [{"user_id": "user-1", "drink_id": "shot-vodka"}]
    blocked = asyncio.run(server.shake_a_shot(use_cupboard=True, authorization="Bearer session"))
    assert blocked.drink is None


def test_only_eligible_shot_can_repeat(monkeypatch):
    configure(monkeypatch)
    selected = asyncio.run(server.shake_a_shot(location_id="loc-one", previous_drink_id="shot-rum", authorization=None))
    assert selected.drink.id == "shot-rum"


def test_zero_eligible_shots_returns_an_empty_response(monkeypatch):
    configure(monkeypatch)
    selected = asyncio.run(server.shake_a_shot(location_id="no-eligible-shots", authorization=None))
    assert selected.drink is None
