# DrinkThink Canonical MongoDB Schema --- v1.1 Correction

**Status:** LOCKED correction\
**Date:** 2026-09-28

This correction supersedes the `ingredient_id integer` statements in the
original locked schema.

## Canonical ingredient identity

`ingredient_id` is a **string**, preserving the IDs already used by the
DrinkThink ingredient hierarchy and My Cupboard, for example:

-   `beer__ale`
-   `bitters__angostura-bitters`
-   `vodka__vodka`
-   `wine__chardonnay-white-wine`

Primary/group ingredient records may use IDs such as `beer`, `bitters`,
`vodka`, etc.

`parent_ingredient_id` is therefore also `string | null`.

All references to ingredients use these same canonical string IDs: -
`cocktails.main_ingredient_ids` - `cocktail_ingredients.ingredient_id` -
`location_inventory.ingredient_id` -
`location_drinks.missing_ingredient_ids` -
`location_drinks.matched_ingredient_ids` -
`pos_ingredient_mappings.ingredient_id` -
`drink_review_queue.proposed_ingredient_ids` - `user_cupboard.item_ids`

## Reason for correction

The v1 dry-run proved that the deployed hierarchy uses semantic string
IDs rather than integer IDs. Preserving those IDs avoids a needless
remapping layer and preserves existing My Cupboard references.

All other canonical schema decisions remain unchanged.
