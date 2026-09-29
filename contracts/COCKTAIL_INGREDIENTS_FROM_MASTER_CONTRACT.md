# DRINKTHINK — Cocktail Ingredients from Master Contract
# Architecture / Integration Thread → Database Thread
# Status: DRAFT (interface need)
# Date: 2026-09-28
# Depends on: DRINKTHINK_CANONICAL_MONGODB_SCHEMA.md (LOCKED v1)
#             DRINK_ADMISSION_CLEAN_DB_CONTRACT_v2.md (LOCKED v2)

==================================================
PURPOSE
==================================================
Define how master-library recipe data (ordered `ingredient_ids` array
plus human-readable quantity text) is converted into canonical
`cocktail_ingredients` rows with the fields required by the schema:

  - cocktail_id
  - ingredient_id
  - amount          (number | null)
  - unit            (string | null)
  - role            (base | ingredient | modifier | garnish)
  - required        (boolean)
  - sequence        (integer | null)
  - notes           (string | null)

Admission v2 states that approved drinks store resolved ingredient_ids
in structured form, but does not define role / required assignment.
This contract fills that gap for both:
  A) bulk load / migration from drinks_clean (or successor master)
  B) admission approve path when only ordered ingredient_ids are known

==================================================
1. SOURCE DATA (MASTER)
==================================================
Typical master row supplies:

- ordered `ingredient_ids: int[]`     — canonical ingredient identity
- human quantity / ingredients text   — display only unless parsed
- optional shopping tokens            — display / transitional only

Authority rule (from Canonical Schema):
  Recipe requirements are owned by `cocktail_ingredients`,
  not by free-text ingredient fields on `cocktails`.

==================================================
2. SEQUENCE
==================================================
- `sequence` = 1-based index in the master ordered `ingredient_ids`
  array (or the order supplied at admission approve).
- Preserve order exactly; do not reorder by category or name.
- If the same `ingredient_id` appears more than once with different
  roles or positions, each occurrence is a separate row (schema unique
  key includes role + sequence).

==================================================
3. ROLE ASSIGNMENT (DEFAULT POLICY)
==================================================
Until a richer classifier exists, apply this deterministic default:

| Position / signal                         | role        |
|-------------------------------------------|-------------|
| First ingredient in ordered list          | `base`      |
| Subsequent ingredients                    | `ingredient`|
| Explicit garnish marker in source text*   | `garnish`   |
| Explicit modifier / float / dash marker*  | `modifier`  |

\* Garnish / modifier detection is optional and may remain unimplemented
  in v1. If not implemented, every non-first item is `ingredient`.

Rationale:
- Matches common cocktail structure (spirit first).
- Fully deterministic and reviewable.
- Does not invent taxonomy; only labels structure.

Future enhancement (out of scope for this draft):
- Use ingredient category (Primary liquor → base, etc.) or admission
  reviewer override to refine role.

==================================================
4. REQUIRED FLAG (DEFAULT POLICY)
==================================================
Default for master load and auto-structured admission:

- `required = true` for every row produced from master `ingredient_ids`.

Rationale:
- Master clean recipes are treated as complete requirements.
- Optional garnishes can be marked `required = false` later by curation
  or by an explicit admission reviewer action.
- Capability (#10) already has separate policies for secondary liquor /
  mixers / common goods; those policies operate on ingredient identity,
  not on this flag. The `required` flag is reserved for “this line is
  part of the defined recipe.”

Admission reviewer MAY set `required = false` on individual lines
when approving a new drink.

==================================================
5. AMOUNT / UNIT
==================================================
v1 policy (conservative):

- Leave `amount` and `unit` as null when loading from master unless a
  separate, tested quantity parser is approved.
- Human-readable quantity strings remain on the cocktail document
  (`human_ingredients` / shopping text) for display only.
- Do not guess units from free text in the migration path.

Later parser (optional, separate contract):
- May populate `amount` / `unit` when confidence is high.
- Must not block admission or master load if parsing fails.

==================================================
6. NOTES
==================================================
- `notes` may hold residual free-text for that line (e.g. “fresh”,
  “to taste”) when available.
- Otherwise null.

==================================================
7. WRITE PROCEDURE
==================================================
When creating or replacing recipe rows for a `cocktail_id`:

1. Delete existing `cocktail_ingredients` for that `cocktail_id`
   only when the operation is an explicit full replace (migration
   re-load or curator “replace recipe”). Admission of a brand-new
   cocktail simply inserts.
2. Insert one row per ordered ingredient with:
   - cocktail_id
   - ingredient_id
   - sequence
   - role          (per §3)
   - required      (per §4)
   - amount, unit  (per §5)
   - notes         (per §6)
3. Enforce schema uniqueness:
   unique (cocktail_id, ingredient_id, role, sequence).
4. Update parent cocktail `main_ingredient_ids` if the platform uses
   that denormalized field for consumer filters (first N bases /
   primaries — exact rule left to Database if not already defined).

==================================================
8. ADMISSION PATH ALIGNMENT
==================================================
On `drink_review_queue` approve (Admission v2):

- If the reviewer supplies structured lines (ingredient_id + role +
  required), persist those exactly.
- If the reviewer supplies only ordered `proposed_ingredient_ids`,
  apply this contract’s defaults (§2–§5) inside the same controlled
  service operation that inserts `cocktails` + `cocktail_ingredients`.
- Unresolved ingredients continue to block auto-admit per Admission
  policy; this contract does not override that gate.

==================================================
9. INVARIANTS
==================================================
- Every active canonical cocktail that claims a structured recipe MUST
  have ≥ 1 `cocktail_ingredients` row (unless explicitly marked
  recipe-pending by status — Database may define that flag).
- `ingredient_id` values MUST exist in the canonical `ingredients`
  collection.
- Role vocabulary is closed: `base | ingredient | modifier | garnish`.
- This contract does not create vendor-specific or admission-specific
  ingredient taxonomies.

==================================================
10. EXPLICIT NON-GOALS
==================================================
- Parsing free-text recipes into ingredient_ids (owned by ingredient
  interpretation pipeline).
- Capability / substitution logic (owned by #10 + location_settings).
- Mood/slider scores.
- POS price or orderability.

==================================================
11. OWNERSHIP
==================================================
- Architecture: this interface contract (role/required/sequence defaults).
- Database: implementation of load, replace, and admission write path;
  indexes; validation that ingredient_ids exist.
- Admission service: calls the same write path on approve.
- Frontend / Pricing / POS: read `cocktail_ingredients`; never invent
  role or required.

==================================================
CHANGE CONTROL
==================================================
Changes to default role or required policy, or introduction of a
quantity parser, must be proposed in Architecture and executed by
Database. No silent divergence between migration load and admission
approve paths.
