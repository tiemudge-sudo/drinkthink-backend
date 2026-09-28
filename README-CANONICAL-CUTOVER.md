# DrinkThink backend canonical MongoDB cutover

Drop-in replacement for the current backend `server.py`.

## Cutover behavior

- All cocktail count/match/search/detail reads use `db.cocktails`.
- Existing mobile Drink response field names are retained.
- `Drink.id` / `drink_id` values are now canonical string `cocktail_id` values.
- Favorites, Blocked and Pending Shares store those canonical string values in the existing `drink_id` field.
- `/api/ingredients` is projected from canonical `ingredient_categories` + `ingredients`.
- Legacy startup seeding from `data/drinks.json` and `data/ingredients_tree.json` is removed.
- Canonical knowledge indexes are ensured at startup.
- Hospitality application and Share Check-In code is unchanged.

## Important frontend contract note

The route names and JSON property names stay the same, but cocktail identity has intentionally changed from integer to string because the canonical migration assigned new `cocktail_id` values. Any frontend TypeScript type that declares `id` or `drink_id` as `number` must be changed to `string`.

## Still intentionally deferred

Structured `cocktail_ingredients` recipe resolution remains a separate data-normalization task. Until then, main-ingredient filtering continues to use the migrated human-readable ingredient/shopping text, matching prior behavior.
