# DrinkThink backend canonical filters cutover

Base: previously validated `DrinkThink-backend-canonical-cutover.zip`.

Changes are limited to filter/cupboard integration with the canonical master applied by `master-canonical-correction-v2`:

- `/api/drinks/match` keeps existing `alcohols` and `glasses` query parameters.
- Main Ingredient uses `cocktails.main_ingredient_ids` -> canonical `ingredients.primary_ingredient`.
- Non-Alcoholic continues to use canonical `cocktails.alcohol_class`.
- Drink Style uses canonical `glass_id`; master-created rows may normalize their retained `migration.legacy_glass` through the already-established glass map.
- Authenticated cupboard filtering reads `user_cupboard.active` and canonical integer item IDs, then evaluates required `cocktail_ingredients`.
- Ingredient satisfaction is exact ID OR same `(primary_category, primary_ingredient)`, per Ingredient Architecture.
- `location_id` now constrains results to `location_drinks.can_make=true` and suppresses home-cupboard filtering while checked in.
- `/api/ingredients` projects the new flat integer master into the existing Category -> Primary Ingredient -> Item UI shape; leaf IDs are stringified integers to avoid a frontend type change.
- `/api/me/cupboard` POST rejects non-integer/unknown ingredient IDs.

Important transition note: cupboard selections saved before the integer-master correction may contain obsolete semantic string IDs. They are not guessed/remapped. The user must reselect those items from the canonical cupboard hierarchy.

No changes to slider scoring, favorites pinning, blocked handling, auth, ordering, pricing, or frontend code.
