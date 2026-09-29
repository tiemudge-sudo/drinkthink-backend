# DrinkThink canonical cocktail_ingredients v1

Purpose: populate the structured recipe relationship required for safe My Cupboard filtering and derive canonical `cocktails.main_ingredient_ids` for What I Want.

## Safety rule
Only fully resolved recipes are written. A partial ingredient list must never be treated as a complete recipe because that could make My Cupboard claim a drink can be made when an unresolved ingredient is actually required.

The resolver first uses `ingredient_ids` / `ingredient_unmatched` from the exact legacy Mongo source document when present. It falls back to the bundled curated resolution CSV by **legacy ID + normalized cocktail name**. The name component prevents ambiguous old duplicate numeric IDs from being assigned blindly.

## Run dry-run first
From the backend repo with the normal `MONGO_URL` and `DB_NAME` environment variables available:

    python tools/build_cocktail_ingredients_v1.py --report cocktail-ingredients-report-v1.json

Review the report. It gives exact resolved/unresolved coverage and unresolved reasons. Dry-run makes no database changes.

## Apply
Only after the dry-run is accepted:

    python tools/build_cocktail_ingredients_v1.py --apply --confirm-apply BUILD_COCKTAIL_INGREDIENTS_V1 --report cocktail-ingredients-apply-v1.json

Apply rebuilds `cocktail_ingredients`, marks recipe resolution status on cocktails, derives `main_ingredient_ids`, and creates indexes. It does not modify user collections.

## Next step
Do not wire My Cupboard to partial/unresolved recipes. After apply validation, update `/api/drinks/match` so What I Want uses `main_ingredient_ids` and My Cupboard uses the complete rows in `cocktail_ingredients`.
