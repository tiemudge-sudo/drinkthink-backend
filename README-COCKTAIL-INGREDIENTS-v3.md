# DrinkThink cocktail_ingredients v3

This package follows the locked ingredient architecture. It does not parse recipe text or invent ingredient mappings.

## Inputs
- `data/drinks_clean_100pct.csv` — authoritative recipe assignments.
- `data/All-ingredients.csv` — canonical integer ingredient identity.
- `data/ingredient_id_merge_map.json` — retired-ID redirects.
- `data/ingredient_lookup.json` — validates canonical IDs are represented by the deterministic surface lookup.

## Acceptance gate
Every ingredient reference on every authoritative CSV cocktail must validate. Apply is blocked unless coverage is exactly 100% and every recipe has exactly one canonical cocktail bridge.

A source ID is resolved by the merge map when present. Because the supplied merge map ends before some newer All-ingredients IDs, a source ID absent from the merge map is accepted as an identity only when the same integer exists in both `All-ingredients.csv` and the values of `ingredient_lookup.json`. This is reported as `master_plus_lookup_identity`.

The script also validates that the current Mongo `ingredients` collection contains the resulting canonical integer IDs. If the deployed DB still contains a different ingredient-ID representation, dry-run will report that mismatch and APPLY WILL BE BLOCKED.

## Dry run
From the package root with the backend Mongo environment available:

```bash
python tools/build_cocktail_ingredients_v3.py --report cocktail-ingredients-report-v3.json
```

## Apply
Only after the dry-run report shows `apply_gate_passed: true`:

```bash
python tools/build_cocktail_ingredients_v3.py --apply --confirm-apply BUILD_COCKTAIL_INGREDIENTS_V3 --report cocktail-ingredients-apply-v3.json
```

Apply replaces recipe relationships only for cocktails represented by the authoritative CSV. It does not modify source cocktails, the ingredient taxonomy, or cocktails absent from the CSV.
