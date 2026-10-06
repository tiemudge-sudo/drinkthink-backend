# DrinkThink — Vendor onboarding contract

**Contract status:** Locked through the canonical-reference export stage
**Recorded:** October 6, 2026
**Database:** `DrinkThinkv0`
**Scope:** Reusable contract for each newly onboarded vendor location

This checkpoint records the approved onboarding path. Menu ingestion, catalog activation, and consumer location discovery are not complete at this stage.

## 1. Source application

Each vendor begins with one contracted `hospitality_partner_applications` record containing the approved business, location, physical address, partnership path, and POS status. The application remains the lead/contract record.

## 2. Required address enrichment and onboarding transition

Before creating a location record, submit the contracted physical address to the backend provisioning endpoint. The endpoint owns the Geocodio call, address validation/normalization, GeoJSON ordering, and persistence.

```text
Vendor physical address
  → POST /api/admin/locations
  → backend Geocodio validation / normalization
  → normalized address + GeoJSON Point persisted in locations
```

```text
hospitality_partner_applications
  → organizations
  → locations
  → vendor_users
  → organization_members
  → location_settings
```

Create stable `org_*` and `loc_*` identifiers; mark the organization, vendor user, membership, and settings records `active`; assign the vendor contact as `primary_contact`. Create the location through `POST /api/admin/locations` as the location-creation step, then verify its normalized address and GeoJSON Point by reading the returned/persisted record. Commit the remaining transition atomically where transactions are supported, then read it back and verify relationships.

For a menu-only vendor, do not create a fake POS connection: `pos_connection_id = null`.

## 3. Vendor-location address enrichment requirements

Geocodio is an **offline vendor-provisioning prerequisite** to location creation, never a consumer runtime dependency. It is invoked only by the backend provisioning endpoint:

```text
POST /api/admin/locations
Authorization: Bearer <DRINKTHINK_LOCATION_PROVISIONING_API_KEY>
```

```text
Vendor physical address
  → Geocodio address validation / normalization
  → latitude and longitude
  → locations.address and locations.geo
  → MongoDB 2dsphere search
  → consumer location/check-in APIs
```

- Supply `location_id`, `organization_id`, name, structured contracted address, IANA timezone, and status to the endpoint.
- Do not supply latitude, longitude, or `geo` in the request.
- The endpoint must persist the Geocodio-normalized address in `locations.address` and coordinates in `geo: { type: "Point", coordinates: [longitude, latitude] }`.
- Never guess, infer, or manually generate coordinates. A location with `geo: null` does not satisfy this contract and must be remediated before consumer proximity discovery/check-in is enabled.
- Consumer clients send device coordinates to the Railway backend; the backend uses MongoDB geospatial search against provisioned locations. Do not call Geocodio during consumer check-in or nearby-location requests.

### Railway development execution and verification

Link the local backend checkout to the DrinkThink backend service in Railway's `development` environment. Invoke provisioning through `railway run` so the CLI injects `DRINKTHINK_LOCATION_PROVISIONING_API_KEY` only into the command runtime. Never copy that value into source code, MongoDB, generated files, logs, prompts, or the frontend.

Development invocation:

```text
POST https://drinkthink-backend-development.up.railway.app/api/admin/locations
Authorization: Bearer <runtime-injected provisioning key>
Content-Type: application/json
```

The request body contains only `location_id`, `organization_id`, name, structured address, IANA timezone, and `active`/`inactive` status. It must not contain latitude, longitude, `geo`, POS fields, or test/radius fields.

After success, read the canonical MongoDB `locations` record and verify:

1. `location_id` and `organization_id` are correct.
2. `address` is the backend/Geocodio-normalized address.
3. `geo.type` is `Point` and `geo.coordinates` contains valid numeric `[longitude, latitude]` values.
4. Status, timezone, and `pos_connection_id` retain their intended values.
5. An existing location retains `created_at`; only `updated_at` changes.

The same endpoint is the remediation path for an existing location with missing or invalid `geo`. It must geocode successfully before updating the canonical record; never repair this condition with a direct MongoDB update.

## 4. Menu-only operating model

```ini
dt_availability = menu_only
catalog_mode = manual
inventory_mode = off
inventory_source.kind = menu_catalog
pos_connection_id = null
```

The vendor menu is the authoritative location catalog, not a new cocktail master.

`menu_catalog` means effective ingredient evidence is derived only from the
active, canonically reconciled catalog drinks and their
`cocktail_ingredients` recipes. It does not use menu free text as ingredient
identity, create `location_inventory` rows, or use a fake POS connection.
For a menu-only location, capability results must remain limited to the active
catalog; deriving a menu ingredient set cannot surface other master cocktails.
Do not rebuild `location_drinks` until the reviewed catalog mappings exist.

```text
Vendor menu → manual catalog ingest → inbound_items → canonical cocktail matching
  → confident match: cocktail_id
  → unresolved: review → existing canonical cocktail or Drink Admission
  → location_catalog_items → location_drinks
```

### Location capability availability policy

The Vendor Settings Database Contract stores capability assumptions on each
location's `location_settings` document. They are not organization settings,
location fields, inventory fields, or ingredient attributes. An
`organization_id` may be denormalized on the document, but the policy belongs
to `location_id`.

```text
secondary_liquor_availability: explicit | assumed_available
mixer_availability:             explicit | assumed_available
```

For backward compatibility, a missing value for either field means
`explicit`. This is a locked runtime fallback for existing or unmigrated
locations, not an onboarding default and not a reason to backfill documents.

| Canonical ingredient category | Capability treatment |
|---|---|
| `primary_liquor` | explicit availability required |
| `secondary_liquor` | `secondary_liquor_availability` |
| `wine` | explicit availability required |
| `beer` | explicit availability required |
| `mixer` | `mixer_availability` |
| `common_items` | assumed available |

Assumptions affect only capability interpretation. They never create
synthetic inventory rows or evidence that a location physically stocks an
ingredient. A future settings write API or model must enum-validate both
fields and reject unknown values.

## 5. Reconciliation rule

- Auto-match only through a deterministic, unambiguous canonical match (for example, exact normalized name or approved alias).
- Fuzzy matching can suggest candidates but cannot create a vendor-item-to-cocktail relationship.
- A genuinely new drink completes Drink Admission, including its canonical recipe and `cocktail_ingredients`, before cataloging.
- Vendor name and price belong to the location catalog; canonical drink identity and recipe remain in DrinkThink master data.

### Required inbound-provenance handoff

Manual ingestion remains the source-evidence stage even when an external
reconciliation provider performs the menu matching. Before importing
`location_catalog_items`, the handoff must include one persisted
`inbound_items` record for every vendor menu item, with the exact
`inbound_item_id` referenced by the catalog mapping.

Each inbound record must preserve the vendor-origin evidence needed to repeat
and audit reconciliation: location and organization identity, manual-menu
source metadata, raw vendor item name, available description/category/section,
price and modifiers when supplied, and the ingestion/review status. It must
not be a placeholder containing only an ID.

The controlled sequence is:

```text
source menu evidence
  → inbound_items
  → external or internal reconciliation/review
  → canonical cocktail_id decision
  → location_catalog_items
```

`location_catalog_items.inbound_item_id` must resolve to its corresponding
inbound record before import. MongoDB does not enforce this foreign-key-like
relationship, so the import process must validate it explicitly. A catalog
mapping without inbound provenance is incomplete even when its
`cocktail_id` is otherwise valid.

## 6. Canonical-reference export stage

Before manual-menu reconciliation, export import-ready MongoDB Extended JSON for the vendor onboarding records and `cocktails`, `cocktail_ingredients`, `ingredients`, `ingredient_categories`, `ingredient_id_merges`, `glass_categories`, and `glasses`. Preserve identifiers and timestamps and include SHA-256 hashes in the manifest.

## 7. Remaining steps

1. Create `inbound_items`, `location_catalog_items`, and `location_drinks`.
2. Add and validate menu-only location settings.
3. Obtain and normalize the vendor menu.
4. Reconcile canonical matches and review exceptions.
5. Complete Drink Admission for new drinks.
6. Populate and activate the reviewed catalog.

## 8. Guardrails

- Do not modify frontend, production deployment, or glass-mapping work as part of this contract.
- Do not create vendor-local cocktail identities.
- Do not create menu/catalog records before review.
- Do not modify canonical cocktail, ingredient, or recipe data during reference export.
- Geocodio is provisioning-only.
