# DrinkThink Canonical MongoDB Schema --- v1.2

## Identity correction

The pre-production migration removes `legacy_drink_id` as a canonical
cocktail business key.

`cocktails.cocktail_id` is the only canonical cocktail identity. Every
existing source cocktail receives a new unique canonical ID. Old numeric
drink IDs, source Mongo IDs, and old glass values may be retained under
`migration` solely for provenance during the transition and are not part
of the application contract.

Ingredient identity remains the semantic string `ingredient_id` already
used by the DrinkThink ingredient hierarchy and My Cupboard.

## Cocktail identity

Required: - `cocktail_id`: unique string - `name` - `normalized_name` -
canonical recipe/presentation/scoring fields from the locked schema

Not canonical: - old numeric `drink_id` - `legacy_drink_id`

## Pre-production reset rule

Because there are no production users, test-only user-domain state
(`favorites`, `blocked`, `pending_shares`, `user_cupboard`) may be reset
during this migration rather than carrying ambiguous legacy cocktail
references forward.

All other canonical architecture decisions remain unchanged.
