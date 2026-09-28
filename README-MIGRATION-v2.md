# DrinkThink MongoDB Migration v2

The v1 dry-run successfully found two assumptions that must be corrected before any write:

1. Ingredient IDs are canonical semantic **strings**, not integers.
2. Legacy drink IDs `1` through `7` are duplicated, so they cannot yet satisfy the canonical unique `legacy_drink_id` constraint.

v2 fixes the ingredient interpretation and improves diagnostics. It does **not** guess how to resolve duplicate drink IDs.

Run only the dry-run now:

```bat
python tools\migrate_canonical_schema_v2.py --report migration-report-v2.json
```

Do not use `--apply`.

The v2 report will:
- preserve the 1,333 hierarchy IDs as canonical strings;
- validate My Cupboard against those exact IDs;
- show the names/Mongo `_id`s of every document in duplicate legacy-ID groups;
- summarize distinct legacy glass values instead of producing one glass exception per drink;
- calculate an exact canonical bridge for every unambiguous legacy drink.

`--apply` additionally requires `--confirm-apply CANONICAL_V2`, but it must not be used until the duplicate IDs and remaining exceptions are reviewed.
