# DRINKTHINK — Legacy Drink ID Bridge Contract
# Architecture / Integration Thread → Database Thread
# Status: DRAFT (interface need)
# Date: 2026-09-28
# Depends on: DRINKTHINK_CANONICAL_MONGODB_SCHEMA.md (LOCKED v1)

==================================================
PURPOSE
==================================================
Define the authoritative mapping between the master library
(drinks_clean_100pct.csv numeric `id`) and the canonical
`cocktails` collection (`cocktail_id` string + optional
`legacy_drink_id` integer).

This contract exists because:
- Canonical Schema defines the `legacy_drink_id` field but does not
  define how it is populated from the master CSV.
- Dry-run migration showed that looking up CSV `id` as
  `legacy_drink_id` does not reliably find the canonical cocktail
  (many `ckt_...` rows were generated from old Mongo `_id` values,
  not from drinks_clean IDs).
- Name-based matching is explicitly disallowed unless this contract
  authorizes it under stated conditions.

==================================================
1. AUTHORITATIVE IDENTIFIERS
==================================================

| System                         | Identifier              | Type    | Notes                          |
|--------------------------------|-------------------------|---------|--------------------------------|
| Master library (CSV)           | `id`                    | integer | Source of truth for curated set |
| Canonical cocktails            | `cocktail_id`           | string  | e.g. `ckt_...` — runtime PK    |
| Canonical cocktails (bridge)   | `legacy_drink_id`       | integer | Nullable, unique when present  |
| Legacy consumer API            | `drink_id` / `id`       | integer | May still be exposed during cutover |

Runtime systems MUST prefer `cocktail_id`.
`legacy_drink_id` is a migration/compatibility bridge only.

==================================================
2. POPULATION RULE (REQUIRED)
==================================================
When a canonical `cocktails` document is created or updated from the
master library (bulk load, re-seed, or curated import):

1. Set `legacy_drink_id` = the master CSV `id` for that row.
2. `legacy_drink_id` MUST be unique across the collection when present
   (already required by Canonical Schema sparse unique index).
3. If a `cocktail_id` already exists for that master row, UPDATE the
   existing document’s `legacy_drink_id` rather than inserting a duplicate.
4. Never invent a `legacy_drink_id` that does not appear in the current
   master CSV (or an approved successor master artifact).

Provenance recommendation (optional but preferred):
- Store `source = "curated"` (or equivalent) on cocktails loaded from master.
- Optionally store `master_version` / import batch id for audit.

==================================================
3. LOOKUP CONTRACT (READ PATH)
==================================================
To resolve a master numeric ID to a canonical cocktail:

```
canonical = cocktails.find_one({ "legacy_drink_id": <master_id> })
```

- If found → use that `cocktail_id`.
- If not found → treat as **unresolved**. Do NOT fall back to name match
  unless Section 4 explicitly allows it for the calling context.

POS and admission paths that already hold a `cocktail_id` never need
this bridge.

==================================================
4. NAME-BASED MATCHING (DEFAULT: FORBIDDEN)
==================================================
Default policy: **name-only matching is forbidden** for the purpose of
linking master CSV rows to existing `cocktails` documents.

Exceptions require an explicit, written policy in this contract or a
successor. Proposed (not yet locked) exception conditions:

- Both sides have identical `normalized_name`, AND
- No other cocktail shares that `normalized_name`, AND
- The operation is a one-time migration tool with human review of
  collisions, AND
- The tool writes `legacy_drink_id` only after review confirmation.

Until such an exception is locked, implementers MUST NOT match by name.

==================================================
5. MIGRATION / BACKFILL PROCEDURE (DATABASE OWNS EXECUTION)
==================================================
Suggested ordered steps (Database thread executes; Architecture only
defines the interface):

1. Load current master `drinks_clean_100pct.csv` (or successor).
2. For each master row with `id = N`:
   a. If a cocktail exists with `legacy_drink_id = N` → ensure fields
      are consistent; no duplicate insert.
   b. Else if a cocktail exists that was previously known to be that
      master row (via internal migration map, if any) → set
      `legacy_drink_id = N`.
   c. Else → create new cocktail with new `cocktail_id` and
      `legacy_drink_id = N` (and recipe rows per the companion
      COCKTAIL_INGREDIENTS_FROM_MASTER_CONTRACT).
3. Report:
   - count of master IDs successfully bridged
   - count of master IDs with no canonical row (gap)
   - count of canonical rows that still lack `legacy_drink_id`
4. Gaps are resolved by curated insert or explicit “no bridge” decision;
   they are never silently filled by name match.

==================================================
6. INVARIANTS
==================================================
- One master `id` maps to at most one `cocktail_id`.
- One `cocktail_id` has at most one `legacy_drink_id`.
- Absence of `legacy_drink_id` does not make a cocktail invalid; it only
  means the legacy/master bridge is missing.
- Consumer favorites/blocked/shares that still key on numeric IDs use
  `legacy_drink_id` (or a temporary dual-write) until cutover to
  `cocktail_id` is complete (see Canonical Schema §9 / §11).

==================================================
7. EXPLICIT NON-GOALS
==================================================
- Does not redefine `cocktail_id` generation.
- Does not authorize POS provider IDs as `legacy_drink_id`.
- Does not define mood/score population.
- Does not define admission of brand-new POS drinks (see Admission v2).

==================================================
8. OWNERSHIP
==================================================
- Architecture: this interface contract.
- Database: population, backfill, indexes, gap reports.
- Admission / POS: consume `cocktail_id`; use bridge only when starting
  from a master numeric ID.
- Frontend: continues to receive whatever ID the API contract exposes
  during migration; never invents bridge logic.

==================================================
CHANGE CONTROL
==================================================
Any change to population rules, lookup rules, or name-match policy
must be proposed in Architecture and executed by Database.
No parallel mapping tables outside this bridge.
