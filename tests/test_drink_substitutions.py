"""Detail-response coverage for existing ingredient alias substitutions."""

import asyncio
import copy
import os
from types import SimpleNamespace

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "drinkthink_test")

import server


class Cursor:
    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)

    async def to_list(self, length=None):
        return self.rows if length is None else self.rows[:length]


class Collection:
    def __init__(self, rows=()):
        self.rows = list(copy.deepcopy(rows))

    @staticmethod
    def _matches(row, query):
        for field, expected in query.items():
            actual = row.get(field)
            if isinstance(expected, dict) and "$in" in expected:
                if actual not in expected["$in"]:
                    return False
            elif actual != expected:
                return False
        return True

    def find(self, query, _projection=None):
        return Cursor([row for row in self.rows if self._matches(row, query)])


def configure(monkeypatch, *, relationships, ingredients, merges=()):
    monkeypatch.setattr(
        server,
        "db",
        SimpleNamespace(
            cocktail_ingredients=Collection(relationships),
            ingredients=Collection(ingredients),
            ingredient_id_merges=Collection(merges),
        ),
    )


def cocktail(tokens):
    return {
        "cocktail_id": "cocktail-1",
        "shopping_tokens": tokens,
    }


def ingredient(ingredient_id, name, aliases):
    return {
        "ingredient_id": ingredient_id,
        "name": name,
        "aliases": aliases,
        "status": "active",
    }


def test_detail_substitution_uses_existing_alias_and_canonical_name(monkeypatch):
    configure(
        monkeypatch,
        relationships=[{"cocktail_id": "cocktail-1", "ingredient_id": 1481, "sequence": 1}],
        ingredients=[ingredient(1481, "Raspberry Liqueur", ["chambord"])],
    )

    assert asyncio.run(server._drink_substitutions(cocktail("Chambord"))) == [
        {"original": "Chambord", "classification": "Raspberry Liqueur"},
    ]


def test_detail_substitutions_follow_recipe_sequence_and_deduplicate(monkeypatch):
    configure(
        monkeypatch,
        relationships=[
            {"cocktail_id": "cocktail-1", "ingredient_id": 1097, "sequence": 2},
            {"cocktail_id": "cocktail-1", "ingredient_id": 1481, "sequence": 1},
            {"cocktail_id": "cocktail-1", "ingredient_id": 1481, "sequence": 1},
        ],
        ingredients=[
            ingredient(1481, "Raspberry Liqueur", ["chambord"]),
            ingredient(1097, "Orange Liqueur", ["cointreau"]),
        ],
    )

    assert asyncio.run(server._drink_substitutions(cocktail("Chambord|Cointreau"))) == [
        {"original": "Chambord", "classification": "Raspberry Liqueur"},
        {"original": "Cointreau", "classification": "Orange Liqueur"},
    ]


def test_detail_substitutions_hide_identical_and_incomplete_mappings(monkeypatch):
    configure(
        monkeypatch,
        relationships=[
            {"cocktail_id": "cocktail-1", "ingredient_id": 1, "sequence": 1},
            {"cocktail_id": "cocktail-1", "ingredient_id": 2, "sequence": 2},
            {"cocktail_id": "cocktail-1", "ingredient_id": "invalid", "sequence": 3},
        ],
        ingredients=[
            ingredient(1, "Vodka", ["vodka"]),
            {"ingredient_id": 2, "aliases": ["mystery"], "status": "active"},
        ],
    )

    assert asyncio.run(server._drink_substitutions(cocktail("Vodka|Mystery|Unknown"))) == []


def test_detail_substitutions_resolve_retired_relationship_ids(monkeypatch):
    configure(
        monkeypatch,
        relationships=[{"cocktail_id": "cocktail-1", "ingredient_id": 999, "sequence": 1}],
        ingredients=[ingredient(1481, "Raspberry Liqueur", ["chambord"])],
        merges=[{"from_id": 999, "resolved_to_id": 1481, "status": "active"}],
    )

    assert asyncio.run(server._drink_substitutions(cocktail("Chambord"))) == [
        {"original": "Chambord", "classification": "Raspberry Liqueur"},
    ]
