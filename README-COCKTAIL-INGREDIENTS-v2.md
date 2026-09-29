# DrinkThink cocktail_ingredients v2

Corrects v1's numeric-ID mismatch by translating curated legacy ingredient IDs through the original ingredient catalog into the canonical semantic string IDs already stored in MongoDB.

## Dry run first

```bash
python tools/build_cocktail_ingredients_v2.py --report cocktail-ingredients-report-v2.json
```

This makes no database changes. Send the report back for review before applying.

## Apply (only after dry-run review)

```bash
python tools/build_cocktail_ingredients_v2.py --apply --confirm-apply BUILD_COCKTAIL_INGREDIENTS_V2 --report cocktail-ingredients-apply-v2.json
```

Only fully resolved recipes are written to `cocktail_ingredients`; unresolved/partial recipes remain excluded from cupboard capability filtering. The script also derives `cocktails.main_ingredient_ids` for the five primary spirits.
