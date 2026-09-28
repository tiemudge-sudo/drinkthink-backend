# DrinkThink --- MongoDB Legacy → Canonical Migration Contract

**Status:** LOCKED v1\
**Date:** 2026-09-28\
**Depends on:** `DRINKTHINK_CANONICAL_MONGODB_SCHEMA.md`\
**Purpose:** Define the additive, reversible migration from the
currently deployed MongoDB model to the locked canonical schema without
breaking the existing consumer API.

## 1. Migration principles

1.  Canonical collections are created alongside legacy collections.
2.  No production legacy collection is dropped during initial
    backfill/cutover.
3.  Existing mobile API response/request contracts remain stable during
    migration.
4.  Canonical business IDs are introduced without abruptly invalidating
    legacy numeric drink IDs.
5.  Every backfill is deterministic and idempotent.
6.  Validation must pass before reads are switched.
7.  Dual-read/compatibility code is temporary and explicitly removable.
8.  Canonical production collections are never drop/reseeded from
    bundled JSON.
9.  User-domain records are preserved; only their references are
    migrated.
10. A failed migration can be rolled back by returning API reads to the
    legacy source until canonical writes have become authoritative.

## 2. Current → canonical collection map

  ---------------------------------------------------------------------------------------
  Current source                       Canonical target           Migration action
  ------------------------------------ -------------------------- -----------------------
  `drinks`                             `cocktails`                Backfill one canonical
                                                                  cocktail per legacy
                                                                  drink

  `drinks.ingredients` /               `cocktail_ingredients` +   Resolve legacy
  `drinks.shopping`                    `ingredients` refs         text/tokens to
                                                                  canonical ingredient
                                                                  IDs; do not treat free
                                                                  text as recipe
                                                                  authority after cutover

  `ingredients_tree`                   `ingredients`,             Backfill ontology
                                       `ingredient_categories`    preserving existing
                                                                  All-ingredients IDs;
                                                                  retire tree as
                                                                  authority

  `favorites`                          `favorites`                Add `cocktail_id`;
                                                                  retain `drink_id`
                                                                  during compatibility
                                                                  window

  `blocked`                            `blocked`                  Add `cocktail_id`;
                                                                  retain `drink_id`
                                                                  during compatibility
                                                                  window

  `pending_shares`                     `pending_shares`           Add `cocktail_id`;
                                                                  retain `drink_id`
                                                                  during compatibility
                                                                  window

  `user_cupboard`                      `user_cupboard`            Normalize `item_ids` as
                                                                  canonical integer
                                                                  `ingredient_id` values

  canonical/test location data         `organizations`,           Populate canonical
                                       `locations`,               location master;
                                       `location_settings`        preserve existing
                                                                  `location_id` values

  `share_checkins`                     `share_checkins`           No structural
                                                                  replacement; validate
                                                                  `location_id` against
                                                                  canonical locations

  `users`, `user_sessions`             same                       No migration beyond
                                                                  indexes/validation

  `app_config`                         same                       No migration

  `hospitality_partner_applications`   same                       No migration
  ---------------------------------------------------------------------------------------

## 3. Cocktail field mapping

Legacy `db.drinks` currently exposes:

-   `id`
-   `name`
-   `category`
-   `alcohol`
-   `glass`
-   `ingredients`
-   `instructions`
-   `shopping`
-   `fancy`
-   `dark`
-   `thirsty`
-   `calm`
-   `celebrate`

Map each row as follows:

  -----------------------------------------------------------------------
  Legacy                  Canonical `cocktails`   Rule
  ----------------------- ----------------------- -----------------------
  `id`                    `legacy_drink_id`       Preserve exactly;
                                                  unique sparse index

  ---                     `cocktail_id`           Allocate stable
                                                  canonical string ID;
                                                  once allocated never
                                                  regenerate

  `name`                  `name`                  Preserve display value

  `name`                  `normalized_name`       Deterministic
                                                  normalization

  `category`              `category`              Preserve initially

  `instructions`          `instructions`          Preserve

  `ingredients`           `human_ingredients`     Preserve for
                                                  display/reference

  `shopping`              `shopping_tokens`       Preserve initially as
                                                  transitional metadata

  `alcohol`               `alcohol_class`         Preserve initially

  `glass`                 `glass_id`              Map through canonical
                                                  `glasses`; unresolved
                                                  values must be
                                                  reported, not guessed

  inferred from           `main_ingredient_ids`   Populate only from
  ingredient mapping                              canonical ingredient
                                                  resolution

  `fancy`                 `scores.fancy`          Direct legacy score
                                                  bridge

  `dark`                  `scores.strong`         Current consumer
                                                  mapping bridge;
                                                  validate against
                                                  scoring workstream
                                                  before legacy score
                                                  columns are retired

  `thirsty`               `scores.thirsty`        Direct legacy score
                                                  bridge

  `calm`                  `scores.comfort`        Current consumer
                                                  mapping bridge

  `celebrate`             `scores.party`          Current consumer
                                                  mapping bridge

  ---                     `source`                `curated` for the
                                                  existing curated
                                                  library

  ---                     `status`                `active` unless
                                                  explicitly excluded

  ---                     timestamps              migration timestamp if
                                                  source has none
  -----------------------------------------------------------------------

**Important:** The score mapping above preserves the behavior of the
currently deployed recommendation API. It does not redefine the scoring
algorithm. Any future scoring redesign is a separate
contract/workstream.

## 4. Canonical cocktail ID allocation

Canonical IDs must not depend on Mongo `_id`.

For each legacy drink:

1.  If a canonical cocktail already exists with `legacy_drink_id`, reuse
    its `cocktail_id`.
2.  Otherwise allocate a new stable opaque/string `cocktail_id`.
3.  Persist the legacy bridge permanently for migrated curated drinks.
4.  Re-running migration must never create a second cocktail for the
    same legacy ID.

This bridge lets the API continue accepting `/drinks/{drink_id:int}`
while internally resolving to canonical cocktails.

## 5. Ingredient migration

### 5.1 Ontology

The existing All-ingredients identity is preserved:

`existing ingredient item ID` → `ingredients.ingredient_id`

Do not allocate replacement IDs for already identified ingredients.

Backfill: - top-level category → `ingredient_categories` - ingredient
node → `ingredients` - hierarchy relationship → `parent_ingredient_id` -
normalized names/aliases as supported by source data

### 5.2 Recipe resolution

Legacy cocktail ingredient text is not sufficient to become structured
recipe authority by itself.

For every cocktail:

1.  Parse existing ingredient/shopping source data using the existing
    ingredient lookup/mapping rules.
2.  Resolve tokens to canonical `ingredient_id`.
3.  Create `cocktail_ingredients` only for resolved ingredients.
4.  Record unresolved tokens in migration output/review data.
5.  Do not silently invent an ingredient match.
6.  A cocktail may exist canonically while recipe resolution remains
    incomplete, but it must not be treated as fully capability-ready
    until required recipe ingredients are resolved.

### 5.3 My Cupboard

`user_cupboard.item_ids` currently contains ingredient selections.
Migration must:

1.  Resolve each stored value to a canonical `ingredient_id`.
2.  Write normalized integer IDs.
3.  Preserve `active`.
4.  Report orphan/unresolved IDs.
5.  Keep the consumer `/ingredients` response contract stable during UI
    migration by generating the hierarchy from canonical ingredients or
    a temporary compatibility projection.

## 6. Glass migration

Create/seed canonical `glasses` from the approved DrinkThink glass
vocabulary.

Legacy `drinks.glass` values are mapped to `glass_id` using an explicit
mapping table. Unknown values go to a migration exception report; they
are not automatically coerced.

During compatibility, API serializers may translate canonical `glass_id`
back to the legacy display string expected by the app.

## 7. User-domain reference migration

### `favorites`

Add: - `cocktail_id`

Backfill by: `favorites.drink_id` → `cocktails.legacy_drink_id` →
`cocktails.cocktail_id`

Temporary unique indexes: - existing (`user_id`, `drink_id`) - new
(`user_id`, `cocktail_id`)

During transition, writes should populate both IDs when a legacy bridge
exists.

### `blocked`

Same strategy as `favorites`.

### `pending_shares`

Add `cocktail_id` using the same bridge. Pending-share presentation
should resolve canonical cocktail name after cutover.

The public/mobile DTO may continue returning `drink_id` until the
frontend contract is deliberately versioned.

## 8. Location migration

Preserve existing stable `location_id` values.

For each active DrinkThink location: - create/validate `organization` -
create/validate canonical `locations` record - populate address,
coordinates, timezone, check-in radius, status - populate
`location_settings` - connect current `pos_connection_id` if applicable

Existing Share Check-In records remain valid because their `location_id`
is preserved.

Temporary hard-coded location fallback may remain only until canonical
location records are present and tested; then remove it.

## 9. POS/capability collections

No legacy data is force-migrated into POS collections unless a
trustworthy source exists.

Create empty/indexed canonical collections as required: -
`pos_connections` - `pos_catalog_items` - `pos_ingredient_mappings` -
`pos_drink_mappings` - `pos_sync_log` - `location_inventory` -
`location_drinks`

Populate these through their owning ingest/capability workflows, not by
guessing from legacy cocktail text.

## 10. API compatibility layer

During cutover, existing endpoints remain externally stable.

Examples:

-   `GET /api/drinks/{legacy_drink_id}` resolves canonical cocktail by
    `legacy_drink_id` and serializes the existing `Drink` DTO.
-   `/api/drinks/search` queries canonical `cocktails` but returns
    legacy-compatible fields.
-   `/api/drinks/match` uses canonical cocktail/scoring/filter data but
    preserves current response shape.
-   favorites/blocked/share endpoints may accept legacy integer IDs,
    resolve them to `cocktail_id`, and dual-write both references.
-   `/api/ingredients` returns a compatibility hierarchy built from
    canonical ingredient/category relationships.

No frontend migration is required merely to change MongoDB storage.

## 11. Cutover phases

### Phase A --- Prepare

-   create canonical collections/indexes
-   add migration metadata/indexes
-   disable any possibility of canonical drop/reseed
-   retain legacy reads

### Phase B --- Backfill

-   ingredients/categories/glasses
-   cocktails
-   cocktail_ingredients
-   user-domain canonical references
-   organizations/locations/settings

### Phase C --- Validate

Required checks: - legacy drink count vs migrated cocktail bridge
count - every legacy `id` maps to exactly one `cocktail_id` - no
duplicate `legacy_drink_id` - favorites/blocked/pending shares resolve -
cupboard IDs resolve or appear in exception report - glass values
resolve or appear in exception report - ingredient-resolution coverage
measured - test locations resolve canonically - Share Check-In still
resolves existing locations

### Phase D --- Canonical reads

Switch read paths one domain at a time: 1. search/detail 2.
recommendation/filtering 3. favorites/blocked/pending shares 4.
ingredients/cupboard 5. locations/check-in

Each switch gets regression testing before proceeding.

### Phase E --- Canonical writes

-   user-domain writes populate canonical references
-   new cocktail admission writes only canonical cocktail/recipe
    structures
-   POS ingest writes only canonical POS/location structures

### Phase F --- Retire legacy authority

Only after all production consumers pass: - remove `db.drinks` read
dependency - remove `ingredients_tree` as authority - remove startup
`drinks.json` drop/reseed behavior - remove startup
`ingredients_tree.json` authority - remove temporary hard-coded location
fallback - stop dual-writing legacy references when frontend/API
versioning permits

Physical deletion of old collections is a later explicit operation, not
part of the first cutover.

## 12. Idempotency and migration metadata

Migration tooling should maintain a migration record,
e.g. `schema_migrations`:

-   `migration_id`
-   `version`
-   `status`
-   `started_at`
-   `completed_at`
-   counts
-   validation results
-   exception counts
-   code/version identifier

Backfill operations must use upserts keyed by canonical business
identity.

## 13. Failure / rollback behavior

Before canonical reads are authoritative, rollback means restoring the
affected API service to legacy reads.

After canonical writes become authoritative: - do not overwrite
canonical data from legacy JSON; - fix forward using
migration/reconciliation tooling; - retain legacy data as comparison
evidence until final retirement.

Migration scripts must support dry-run mode and must not delete
production records.

## 14. Acceptance criteria

Migration is ready for canonical cutover only when:

-   every legacy drink has exactly one canonical cocktail bridge;
-   required canonical indexes exist;
-   user favorites/blocked/pending shares retain their relationships;
-   cupboard ingredient selections are preserved or explicitly reported
    as unresolved;
-   canonical locations preserve existing location IDs;
-   current app endpoints return equivalent DTOs;
-   recommendation/filter regression tests pass;
-   Share Drink and Share Check-In regression tests pass;
-   no startup path can drop/reseed canonical collections;
-   migration exception report is reviewed.

## 15. Retirement list

After full acceptance, these become retirement candidates:

-   `drinks`
-   `ingredients_tree`
-   `data/drinks.json` as production authority
-   `data/ingredients_tree.json` as production authority
-   legacy-only `drink_id` references where API compatibility no longer
    requires them
-   temporary `SHAREABLE_LOCATIONS` fallback

Retirement requires a separate explicit approval after production
validation.
