# DrinkThink --- Canonical MongoDB Schema Contract

**Status:** LOCKED v1\
**Date:** 2026-09-28\
**Purpose:** Single schema authority for DrinkThink cocktail knowledge,
ingredient ontology, hospitality locations, POS interpretation,
capability, pricing/orderability, ordering, and commercial API access.

## 1. Contract rules

1.  This document is the schema authority. Feature contracts reference
    these collections and fields rather than redefining them.
2.  MongoDB `_id` remains an internal implementation key. Business logic
    uses the explicit IDs defined below.
3.  Canonical cocktail data is POS-independent.
4.  Canonical ingredient taxonomy is shared by recipes, My Cupboard, POS
    interpretation, inventory, and capability.
5.  Raw/provider POS data remains separate from DrinkThink
    interpretation.
6.  `can_make`, authorized pricing, and `orderable` are separate states.
7.  Provider credentials/secrets are never stored directly in ordinary
    MongoDB documents; store only a secret reference.
8.  Existing consumer API contracts may continue exposing legacy numeric
    `drink_id` during migration. Canonical storage uses `cocktail_id`;
    `legacy_drink_id` provides the transition bridge.
9.  Timestamps are UTC.
10. No startup process may drop/reseed canonical production collections
    from bundled JSON files.

## 2. ID conventions

  -----------------------------------------------------------------------
  Entity                              Canonical key
  ----------------------------------- -----------------------------------
  Cocktail                            `cocktail_id` string,
                                      e.g. `ckt_...`

  Legacy cocktail bridge              `legacy_drink_id` integer, nullable
                                      but unique when present

  Ingredient                          `ingredient_id` integer ---
                                      preserves the existing
                                      All-ingredients identity

  Ingredient category                 `category_id` string

  Glass                               `glass_id` string

  Organization                        `organization_id` string,
                                      e.g. `org_...`

  Location                            `location_id` string,
                                      e.g. `loc_...`

  POS connection                      `pos_connection_id` string

  POS catalog item                    `pos_catalog_item_id` string

  Order                               `order_id` string

  User                                existing `user_id` string
  -----------------------------------------------------------------------

## 3. Cocktail knowledge

### `cocktails`

Canonical cocktail identity and presentation metadata.

Required/standard fields:

-   `cocktail_id` --- string, required, unique
-   `legacy_drink_id` --- integer, nullable, unique when present
-   `name` --- string, required
-   `normalized_name` --- string, required
-   `category` --- string/null
-   `instructions` --- string/null
-   `human_ingredients` --- string/null; display/reference text only,
    not capability authority
-   `shopping_tokens` --- array/string/null; transitional/display
    metadata only
-   `alcohol_class` --- string/null
-   `glass_id` --- string/null → `glasses.glass_id`
-   `main_ingredient_ids` --- integer\[\]; canonical ingredient
    references used by consumer filters
-   `scores` --- object with nullable `strong`, `fancy`, `comfort`,
    `party`, `thirsty`
-   `source` --- `curated | pos_admit | user_submit | vendor_submit`
-   `admitted_from_review_id` --- string/null
-   `status` --- `active | inactive | review`
-   `created_at`, `updated_at`

**Authority rule:** recipe requirements are owned by
`cocktail_ingredients`, not by free-text ingredient fields.

Indexes: - unique `cocktail_id` - unique sparse `legacy_drink_id` -
`normalized_name` - `status` - `glass_id` - multikey
`main_ingredient_ids`

### `cocktail_ingredients`

Canonical structured recipe requirements.

Fields: - `cocktail_id` - `ingredient_id` - `amount` number/null -
`unit` string/null - `role` ---
`base | ingredient | modifier | garnish` - `required` boolean -
`sequence` integer/null - `notes` string/null

Indexes: - unique (`cocktail_id`, `ingredient_id`, `role`, `sequence`) -
`ingredient_id` - `cocktail_id`

### `ingredients`

Single canonical ingredient ontology.

Fields: - `ingredient_id` integer, required, unique - `name` string -
`normalized_name` string - `category_id` →
`ingredient_categories.category_id` - `parent_ingredient_id`
integer/null → `ingredients.ingredient_id` - `ingredient_type`
string/null - `aliases` string\[\] - `status` --- `active | inactive` -
`created_at`, `updated_at`

A specific ingredient satisfies its ancestors. Example: Maker's Mark →
Bourbon → Whiskey.

Indexes: - unique `ingredient_id` - `parent_ingredient_id` -
`category_id` - `normalized_name` - multikey `aliases`

### `ingredient_categories`

Top-level organizational/UI categories.

Fields: - `category_id` string, unique - `name` - `display_order` -
`status`

### `glasses`

Canonical drink-style/glass vocabulary.

Fields: - `glass_id` string, unique - `name` - `display_name` -
`aliases` string\[\] - `status`

## 4. Hospitality

### `organizations`

Fields: - `organization_id` string, unique - `name` - `type` ---
currently `restaurant` - `status` --- `active | inactive` -
`created_at`, `updated_at`

### `locations`

Fields: - `location_id` string, unique - `organization_id` - `name` -
`address` object: `line1`, `line2`, `city`, `state`, `postal_code`,
`country` - `latitude`, `longitude` --- WGS84 -
`check_in_radius_meters` - `timezone` - `status` ---
`active | inactive` - `pos_connection_id` string/null --- current
one-active-connection shortcut - `created_at`, `updated_at`

Indexes: - unique `location_id` - `organization_id` - `status` -
geospatial `location`/GeoJSON may be added when `/nearby` is implemented

### `location_settings`

Location-owned behavior; organization defaults may be resolved by
service logic. `organization_id` may be denormalized on this document,
but every capability policy belongs to its `location_id`, never to the
organization, location inventory, or ingredient master data.

Fields include: - `location_id` unique - ingest/catalog/inventory
settings - DrinkThink availability settings - ordering/payment
settings - admission policy: - `drink_auto_admit_enabled` default
false - `drink_auto_admit_min_confidence` default 0.95 -
`drink_require_all_ingredients_resolved` default true -
`drink_allow_admit_without_instructions` default true - pricing
references/settings - `updated_at`

#### Capability availability settings

- `secondary_liquor_availability`: `explicit | assumed_available`;
  when absent, runtime behavior is `explicit`.
- `mixer_availability`: `explicit | assumed_available`; when absent,
  runtime behavior is `explicit`.

The absent-field behavior is the locked backward-compatible runtime
fallback for existing or unmigrated location records. It does not require
a document backfill.

| Canonical ingredient `category_id` | Capability treatment |
|---|---|
| `primary_liquor` | explicit availability required |
| `secondary_liquor` | `secondary_liquor_availability` |
| `wine` | explicit availability required |
| `beer` | explicit availability required |
| `mixer` | `mixer_availability` |
| `common_items` | assumed available |

**Inventory Evidence rule:** an assumed-available category changes only
capability interpretation. It never creates synthetic `location_inventory`
records or represents evidence that the location physically stocks an
ingredient.

Any future location-settings write API or model must enum-validate these
two fields and reject unknown values.

### `location_inventory`

Canonical statement of what a location has.

Fields: - `location_id` - `ingredient_id` - `in_stock` boolean -
`quantity` number/null - `unit` string/null - `source` ---
`pos_sync | manual` - `pos_connection_id` string/null - `as_of` -
`updated_by` string/null - `updated_at`

Index: - unique (`location_id`, `ingredient_id`)

### `location_drinks`

Derived/cache layer for location-specific cocktail capability and
orderability.

Fields: - `location_id` - `cocktail_id` - `can_make` boolean -
`missing_ingredient_ids` integer\[\] - `matched_ingredient_ids`
integer\[\] - `has_authorized_price` boolean - `orderable` boolean -
`pricing` object: - `method` ---
`exact_pos | restaurant_defined | pos_construction | restaurant_rule | none` -
`price_cents` integer/null - `rule_id` string/null - `verified`
boolean - `currency` string default `USD` - `computed_at` -
`source_version` string/null

Index: - unique (`location_id`, `cocktail_id`) - (`location_id`,
`can_make`) - (`location_id`, `orderable`)

**Authority rule:** `location_drinks` is derived. It does not redefine
the canonical recipe.

## 5. POS integration

### `pos_connections`

Fields: - `pos_connection_id` unique - `organization_id` -
`location_id` - `provider` - `provider_merchant_id` string/null -
`provider_location_id` string/null - `status` - `credential_reference` -
`configuration` object - `last_catalog_sync_at` -
`last_inventory_sync_at` - `created_at`, `updated_at`

### `pos_catalog_items`

Raw/provider-facing normalized catalog records.

Fields: - `pos_catalog_item_id` unique - `pos_connection_id` -
`provider` - `provider_item_id` - `provider_name` -
`provider_description` string/null - `provider_category` string/null -
`sku` string/null - `price_cents` integer/null - `modifiers`
array/object/null - `active` boolean - `raw_data` object - `updated_at`

Index: - unique (`pos_connection_id`, `provider_item_id`)

### `pos_ingredient_mappings`

Provider item → canonical ingredient.

Fields: - `pos_catalog_item_id` - `ingredient_id` - `confidence`
number - `mapping_method` --- `automatic | rule | human` - `verified`
boolean - `created_at`, `updated_at`

Index: - unique (`pos_catalog_item_id`, `ingredient_id`)

### `pos_drink_mappings`

Provider drink/menu identity → canonical cocktail. This is the canonical
POS source-link mechanism; feature contracts must not create a separate
POS `drink_source_links` schema.

Fields: - `mapping_id` unique - `cocktail_id` - `organization_id` -
`location_id` - `pos_connection_id` - `provider_drink_id` -
`provider_name` - `pos_catalog_item_id` string/null - `mapping_method`
--- `exact | inferred | admitted | human` - `confidence` number/null -
`verified` boolean - `created_at`, `updated_at`

Index: - unique (`pos_connection_id`, `provider_drink_id`) -
(`location_id`, `cocktail_id`)

### `pos_sync_log`

Fields: - `sync_id` unique - `pos_connection_id` - `sync_type` -
`status` - counts/metrics object - `started_at`, `completed_at` -
`error` object/null

## 6. Drink admission workflow

These are workflow collections, not alternate cocktail/ingredient
schemas.

### `drink_review_queue`

Fields: - `review_id` unique - `organization_id` - `location_id`
string/null - `pos_connection_id` string/null - `provider_drink_id`
string/null - `raw_name` - `normalized_name` - `raw_payload` -
`proposed_ingredient_ids` integer\[\] - `ingredient_resolution`
object/array - `unresolved_ingredient_count` - `match_status` ---
`exact_clean | likely_clean | new_drink | insufficient | noise` -
`candidate_cocktail_id` string/null - `match_confidence` number/null -
`admission_recommendation` ---
`auto_admit | review | reject_noise | link_existing` - `status` ---
`open | approved | rejected | linked | deferred` - `reviewer_id`
string/null - `reviewer_notes` string/null - `created_at`, `resolved_at`
null/date

Indexes: - unique `review_id` - (`status`, `created_at`) -
(`location_id`, `status`) - (`pos_connection_id`, `provider_drink_id`)

Ingredient-resolution review should reuse the platform ingredient
ingest/review mechanism rather than creating a second ingredient
taxonomy.

## 7. Ordering

### `orders`

One order maps 1:1 to one captured-payment attempt group at the product
level; payment attempts may be represented separately by the payment
integration when implemented.

Fields: - `order_id` unique - `user_id` - `organization_id` -
`location_id` - `pos_connection_id` string/null - `status` -
`currency` - `submitted_amount_cents` - `pos_final_amount_cents`
integer/null - `created_at`, `submitted_at`, `completed_at`

### `order_items`

Fields: - `order_item_id` unique - `order_id` - `cocktail_id` -
`location_display_name` - `quantity` - `unit_price_cents` -
`pricing_snapshot` - `pos_item_reference` object/null

### `order_events`

Append-only order audit trail.

Fields: - `event_id` unique - `order_id` - `event_type` - `status` -
`payload` - `created_at`

## 8. Commercial API

### `api_clients`

Client identity, owner, status, plan/subscription reference.

### `api_credentials`

Credential metadata/reference only; never plaintext secrets.

### `api_usage`

Client/endpoint/time-window usage and metering.

### `subscriptions`

Plan, status, limits, billing-provider references.

## 9. Existing app-domain collections retained

These are valid application collections and are not replaced merely
because they are outside the platform domains:

-   `users`
-   `user_sessions`
-   `favorites`
-   `blocked`
-   `pending_shares`
-   `user_cupboard`
-   `share_checkins`
-   `app_config`
-   `hospitality_partner_applications`

Reference migration: - `favorites`, `blocked`, `pending_shares`:
canonical field becomes `cocktail_id`; legacy `drink_id` may coexist
during cutover. - `user_cupboard.item_ids`: values remain canonical
`ingredient_id` values. - `share_checkins.location_id`: references
canonical `locations.location_id`.

## 10. Pricing/orderability invariant

1.  Capability answers whether the location can make the cocktail.
2.  Pricing answers whether the restaurant has authorized a price.
3.  Orderability requires capability + authorized price + electronic
    ordering path.
4.  Ingredient availability alone never authorizes a financial
    transaction.
5.  POS confirmation is the final transaction-price authority.

## 11. Migration invariant

During migration: - canonical collections are added alongside legacy
collections; - existing mobile endpoints remain stable; - legacy IDs are
bridged, not abruptly replaced; - no legacy collection is deleted until
every production consumer has cut over and regression tests pass.
