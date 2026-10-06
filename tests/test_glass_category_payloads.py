"""Regression coverage for canonical drink-card glass-category payloads."""

import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "drinkthink_test")

import server


CATEGORIES = (
    "gcat_champagne_flute", "gcat_cordial_glass", "gcat_highball_glass",
    "gcat_margarita_glass", "gcat_martini_glass", "gcat_mug",
    "gcat_pint_glass", "gcat_rocks_glass", "gcat_shot_glass", "gcat_wine_glass",
)


def cocktail(category, glass_id):
    return {
        "cocktail_id": f"drink-{category}", "name": "Category drink", "status": "active",
        "glass_id": glass_id, "glass_category_id": category,
        "scores": {"fancy": 5, "strong": 5, "thirsty": 5, "comfort": 5, "party": 5},
    }


def test_every_canonical_category_is_exposed_in_the_drink_card_payload():
    returned = {
        server._canonical_to_drink(cocktail(category, "different-glass"))["glass_category_id"]
        for category in CATEGORIES
    }
    assert returned == set(CATEGORIES)


def test_drink_card_category_is_independent_of_specific_glass_id():
    category = "gcat_shot_glass"
    assert server._canonical_to_drink(cocktail(category, "legacy-shot"))["glass_category_id"] == category
    assert server._canonical_to_drink(cocktail(category, "another-shot-variant"))["glass_category_id"] == category
