# DrinkThink MongoDB Migration v1

This package implements the first additive backfill step from the legacy MongoDB model to the locked canonical schema.

## Safety

**Dry-run is the default.** The script does not write anything unless `--apply` is supplied.

It never:
- drops a collection;
- deletes a production document;
- rewrites the legacy `drinks` collection;
- guesses unresolved ingredient mappings;
- guesses glass mappings;
- converts legacy free-text recipes directly into authoritative `cocktail_ingredients`.

## Before running

Run this from the backend environment where `MONGO_URL` and `DB_NAME` are already configured, or supply them explicitly.

The script expects the same Python dependencies already used by the backend: `motor`, `pymongo`, and `python-dotenv`.

## Dry run

```bash
python tools/migrate_canonical_schema.py --report migration-report.json
```

Review:
- counts;
- `glass_mapping_required` exceptions;
- non-numeric ingredient IDs;
- orphan favorites/blocked/pending-share references;
- cupboard IDs that do not resolve;
- cocktail count expectations.

## Apply

Only after the dry-run report is accepted:

```bash
python tools/migrate_canonical_schema.py --apply --report migration-apply-report.json
```

The apply pass:
- creates canonical indexes;
- backfills `ingredient_categories` and resolvable canonical `ingredients`;
- backfills `cocktails` with permanent `cocktail_id` + `legacy_drink_id`;
- adds `cocktail_id` references to favorites, blocked, and pending shares;
- normalizes resolvable cupboard IDs;
- records migration state in `schema_migrations`.

## Intentionally deferred

These require explicit mapping/review rather than inference:
1. glass value → canonical `glass_id`;
2. free-text recipe → authoritative `cocktail_ingredients`;
3. `main_ingredient_ids`;
4. location/POS inventory/capability data;
5. switching production API reads from `db.drinks` to canonical collections.

Those are handled after the dry-run report tells us exactly what exists in production.

## Recommended execution sequence

1. Commit this package to the backend repo.
2. Run dry-run against the development database.
3. Review the generated report.
4. Resolve mapping exceptions.
5. Run `--apply` in development.
6. Regression test.
7. Only then repeat the controlled process for preview/production.
