# Share Check-In Public Short-Link API — 2026-09-28

Implemented from `DrinkThink_Share_CheckIn_Public_ShortLink_API_Handoff.md`.

## Changes

- `POST /api/locations/share-check-in` now creates an opaque `j_...` Share ID, persists the private share record, binds it to the authenticated sharer and one location, and returns `share_id`, `share_url`, and `expires_at`.
- New public resolver: `GET /api/public/share-check-ins/{share_id}`.
- Existing `GET /api/locations/shared-check-in` remains compatible with legacy `token=` links and now also accepts `share_id=`.
- New authenticated revocation endpoint: `DELETE /api/locations/share-check-in/{share_id}`.
- Raw signed Share Check-In tokens remain private/server-side and are not returned by the new public resolver.
- `SHARE_CHECKIN_SECRET` is now required for Share Check-In signing; the `ADMIN_TOGGLE_KEY` fallback was removed.
- Production CORS explicitly includes `https://drinkthink.app` and `https://www.drinkthink.app`. Optional extra browser origins can be supplied with comma-separated `CORS_ORIGINS`.
- Share Check-In indexes are created on startup.
- Location resolution checks canonical Mongo `locations` first. The two existing temporary test locations remain as a transitional fallback so the current UI test cycle is not broken; remove this fallback after those records are present in canonical `locations`.

## Railway

Confirm `SHARE_CHECKIN_SECRET` exists (it was already configured in the supplied project context), then deploy this backend version.

## Return point for Website/UI

Create a Share Check-In from the authenticated app. The API should return:

```json
{
  "share_id": "j_...",
  "share_url": "https://drinkthink.app/j/j_...",
  "expires_at": "..."
}
```

Use the real `share_url` or `share_id` for the website end-to-end test.

## Validation performed

`python -m py_compile server.py` passed before packaging.
