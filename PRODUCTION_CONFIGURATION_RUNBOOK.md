# DrinkThink backend production configuration runbook

This document lists configuration names and validation steps only. Never add
credential values to this repository, a MongoDB document, build output, or a
support ticket.

## Required application startup configuration

- `MONGO_URL`: production MongoDB connection string.
- `DB_NAME`: must be exactly `DrinkThinkv0` for the production service.

Before deployment, confirm the Railway production service has both values and
that the selected database is `DrinkThinkv0`. Do not validate by printing the
connection string.

## Optional application configuration

- `CANONICAL_READ_MODEL_CACHE_TTL_SECONDS`: bounded canonical read-model cache
  lifetime. The application defaults to 60 seconds and clamps values to a safe
  range.
- `CORS_ORIGINS`: comma-separated additional allowed origins. The DrinkThink
  web origins are built in.

## Premium verification

Premium verification requires server-side credentials only. Configure all of
the following before enabling store purchases:

- `PREMIUM_PURCHASE_IDENTITY_HMAC_KEY`
- `PREMIUM_VERIFICATION_REFERENCE_ENCRYPTION_KEY`
- `APPLE_APP_STORE_PRIVATE_KEY`
- `APPLE_APP_STORE_ISSUER_ID`
- `APPLE_APP_STORE_KEY_ID`
- `APPLE_BUNDLE_ID`
- `GOOGLE_PLAY_SERVICE_ACCOUNT_JSON`
- `GOOGLE_PLAY_PACKAGE_NAME`

`APPLE_APP_STORE_ENVIRONMENT` is optional and defaults to `Production`. Set it
explicitly only in non-production environments that intentionally verify Apple
sandbox transactions.

## Controlled operational credentials

These credentials are needed only for their protected workflows. Keep every
one distinct and server-side:

- `ADMIN_TOGGLE_KEY`: protected remote-flag updates.
- `DRINKTHINK_LOCATION_PROVISIONING_API_KEY`: canonical location provisioning.
- `DRINKTHINK_LOCATION_INVENTORY_REBUILD_API_KEY`: derived location-drink
  rebuilds.
- `GEOCODIO_API_KEY`: address resolution during provisioning only; it is not a
  consumer discovery or check-in dependency.
- `SHARE_CHECKIN_SECRET`: shared check-in capability.
- `DRINKTHINK_AI_MONGO_URL`, `DRINKTHINK_AI_DB_NAME`, and
  `DRINKTHINK_AI_API_KEY`: optional dedicated read-only AI automation access.
- `RESEND_API_KEY`, `PARTNER_NOTIFICATION_EMAIL`, and
  `PARTNER_NOTIFICATION_FROM`: optional partner-application notifications.

## Database-backed feature and location configuration

Feature flags are not Railway environment variables. In `DrinkThinkv0`, the
`app_config` record with `_id: "flags"` controls:

- `consumer_ordering_enabled`
- `consumer_premium_sales_enabled`

The `app_config` record with `_id: "location_check_in"` controls the global
`check_in_radius_feet`. The canonical v1 radius is 200 feet (60.96 meters for
geospatial evaluation). Validate both records independently; do not replace
one document while changing the other.

## Production validation checklist

1. Confirm the deployed branch/commit is the approved `main` promotion.
2. Confirm Railway production uses `DB_NAME=DrinkThinkv0` and a production
   MongoDB connection without displaying it.
3. Verify `app_config.flags` has the approved ordering and Premium values.
4. Verify `location_check_in.check_in_radius_feet` is 200.
5. Verify the Premium credential names above are present before enabling
   purchases; run a safe invalid-evidence verification test rather than a live
   purchase during configuration validation.
6. Verify protected admin, provisioning, and rebuild credentials are present
   without logging their values.
7. Verify CORS origins include only intended production clients.
8. Confirm no deployment artifact contains `.env` files or credential values.
9. Run the current backend test suite and a post-deploy health/API smoke test.
