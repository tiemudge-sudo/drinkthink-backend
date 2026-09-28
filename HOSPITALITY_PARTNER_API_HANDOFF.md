# Hospitality Partner Form API Contract

## Status
Implemented in the supplied backend source. Deployment is still required.

## Production API base URL
The reviewed repository does not define the deployed public Railway Platform API hostname. Do not invent one.
Use the production Platform API base URL configured by the backend deployment.

## Endpoint
`POST /api/public/hospitality-partner-applications`

Authentication: none.

## Request
```json
{
  "business_name": "Example Restaurant Group",
  "location_name": "Example Restaurant - Tampa",
  "street_address": "123 Main St",
  "city": "Tampa",
  "state_region": "FL",
  "postal_code": "33602",
  "country": "US",
  "contact_name": "Jane Smith",
  "contact_email": "jane@example.com",
  "contact_phone": "+1 813 555 0100",
  "has_multiple_locations": true,
  "partnership_path": "ordering",
  "pos_provider": "toast",
  "comments": "Optional comments"
}
```

`partnership_path` is exactly one of: `ordering`, `drink_menu`, `general_interest`.

`pos_provider` is required when `partnership_path` is `ordering`; otherwise it is optional.

Required fields: `business_name`, `street_address`, `city`, `state_region`, `postal_code`,
`contact_name`, `contact_email`, `has_multiple_locations`, `partnership_path`.

Optional fields: `location_name`, `contact_phone`, `pos_provider`, `comments`.
`country` is optional and defaults to `US`.

## Success
HTTP `201 Created`

```json
{
  "application_id": "hpa_<opaque-server-generated-id>",
  "status": "pending",
  "submitted_at": "2026-09-28T16:48:00Z"
}
```

`application_id`, `status`, and timestamps are server-controlled.

## Persistence
MongoDB collection: `hospitality_partner_applications`.

This endpoint creates only a prospective application. It does not create an organization,
restaurant location, vendor account, POS connection, or operational configuration.

## Errors
FastAPI/Pydantic validation failures: HTTP `422` with the standard FastAPI `detail` array.

Rate limit: HTTP `429`:
```json
{
  "detail": {
    "code": "rate_limit_exceeded",
    "message": "Too many partner applications. Please try again later."
  }
}
```
A `Retry-After` header is returned.

Unexpected server/database failure: HTTP `500`.

## CORS
Production browser origins explicitly allowed:
- `https://drinkthink.app`
- `https://www.drinkthink.app`

Additional development origins can be supplied with `CORS_EXTRA_ORIGINS`.

## Anti-abuse
The implementation includes a per-process burst limit of 5 submissions per source IP per hour
and returns `429` + `Retry-After` when exceeded. Production should additionally enforce an edge
rate limit in Cloudflare/Railway because process-local limits do not coordinate across instances.

The website should preserve entered form values on `429` and allow retry after the indicated delay.
