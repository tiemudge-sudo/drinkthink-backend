"""Focused unit coverage for v3.2 ingredient continuity resolution."""

import asyncio

import pytest

from ingredient_resolution import (
    IngredientResolutionError,
    resolve_ingredient_id,
    resolve_ingredient_id_map,
    resolve_ingredient_ids,
)


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length=None):
        return list(self.rows if length is None else self.rows[:length])


class Collection:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def find(self, query, _projection=None):
        self.queries.append(query)

        def matches(row):
            return all(
                row.get(key) in expected["$in"] if isinstance(expected, dict) and "$in" in expected
                else row.get(key) == expected
                for key, expected in query.items()
            )

        return Cursor([row for row in self.rows if matches(row)])


class Database:
    def __init__(self):
        self.ingredient_id_merges = Collection([
            {"from_id": 1266, "to_id": 1261, "resolved_to_id": 1261, "status": "active"},
            {"from_id": 1269, "to_id": 1261, "resolved_to_id": 1261, "status": "active"},
            {"from_id": 2403, "to_id": 1647, "resolved_to_id": 1647, "status": "inactive"},
        ])
        self.ingredients = Collection([
            {"ingredient_id": 1261, "status": "active"},
            {"ingredient_id": 1647, "status": "active"},
            {"ingredient_id": 2000, "status": "active"},
        ])


def test_batch_resolution_uses_resolved_target_preserves_order_and_deduplicates():
    database = Database()
    resolved = asyncio.run(resolve_ingredient_ids(database, [1266, "1269", 2000, 1266]))
    assert resolved == [1261, 2000]
    assert len(database.ingredient_id_merges.queries) == 1
    assert len(database.ingredients.queries) == 1


def test_single_resolution_and_active_canonical_identity():
    database = Database()
    assert asyncio.run(resolve_ingredient_id(database, 1266)) == 1261
    assert asyncio.run(resolve_ingredient_id(database, "2000")) == 2000


def test_inactive_merge_does_not_redirect_and_unknown_ids_are_rejected():
    database = Database()
    with pytest.raises(IngredientResolutionError, match="2403"):
        asyncio.run(resolve_ingredient_ids(database, [2403]))
    with pytest.raises(IngredientResolutionError, match="9999"):
        asyncio.run(resolve_ingredient_ids(database, [9999]))


def test_historical_read_mode_omits_unknown_values_without_failing():
    database = Database()
    assert asyncio.run(resolve_ingredient_ids(database, [1266, 9999, "bad"], strict=False)) == [1261]
    assert asyncio.run(resolve_ingredient_id_map(database, [1266, 9999], strict=False)) == {1266: 1261}
