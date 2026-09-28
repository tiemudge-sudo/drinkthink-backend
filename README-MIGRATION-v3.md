# DrinkThink Canonical Reset / Migration v3

This is the simplified pre-production migration.

### What it changes

- Creates a new canonical `cocktail_id` for every source Mongo drink document.
- Migrates all 16,359 source drinks; duplicate old numeric IDs no longer matter.
- Keeps old IDs only under `migration` provenance.
- Preserves the 1,333 semantic string ingredient IDs.
- Normalizes legacy glass names into canonical glass IDs.
- Resets test `favorites`, `blocked`, `pending_shares`, and `user_cupboard` on apply.
- Rebuilds canonical cocktail/ingredient/glass collections.
- Does not yet invent structured `cocktail_ingredients`; recipe resolution remains the next data-normalization task.

### First run

```bat
python tools\migrate_canonical_reset_v3.py --report migration-report-v3.json
```

If the report shows only the known composite glass exception (`Shot Glass | Bottle`) and 16,359/16,359 unique new cocktail IDs, the migration is structurally ready.

### Apply

After review:

```bat
python tools\migrate_canonical_reset_v3.py --apply --confirm-apply RESET_CANONICAL_V3 --report migration-apply-v3.json
```

This is intentionally an explicit pre-production reset. It clears/rebuilds canonical target collections and clears test user preference/share collections. It does not drop Mongo collections.
