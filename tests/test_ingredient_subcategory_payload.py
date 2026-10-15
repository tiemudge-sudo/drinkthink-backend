"""Contract coverage for canonical ingredient subcategory navigation data."""

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "drinkthink_test")

import server


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, *_args):
        return self

    async def to_list(self, length=None):
        return self.rows if length is None else self.rows[:length]


class Collection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, query, _projection=None):
        return Cursor([row for row in self.rows if row.get("status") == query.get("status")])


def test_ingredient_catalog_exposes_only_populated_canonical_subcategories(monkeypatch):
    monkeypatch.setattr(
        server,
        "db",
        SimpleNamespace(
            ingredient_categories=Collection([
                {"category_id": "primary_liquor", "name": "Primary Liquor", "status": "active", "display_order": 1},
            ]),
            ingredients=Collection([
                {"ingredient_id": 1, "name": "Dark Rum", "status": "active", "category_id": "primary_liquor", "primary_ingredient": "Rum", "subcategory": "dark rum"},
                {"ingredient_id": 2, "name": "Rum", "status": "active", "category_id": "primary_liquor", "primary_ingredient": "Rum", "subcategory": "   "},
                {"ingredient_id": 3, "name": "White Rum", "status": "active", "category_id": "primary_liquor", "primary_ingredient": "Rum"},
            ]),
        ),
    )

    catalog = asyncio.run(server.get_ingredients_tree())
    items = catalog[0]["primaries"][0]["items"]

    assert [(item["id"], item["subcategory"]) for item in items] == [
        ("1", "dark rum"),
        ("2", None),
        ("3", None),
    ]
