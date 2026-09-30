"""Geocodio-backed, onboarding-only canonical location geocoding."""
import math
from typing import Any

import httpx

GEOCODIO_FORWARD_URL = "https://api.geocod.io/v2/geocode"
MIN_ACCURACY = 0.8
ADDRESS_LEVEL_TYPES = {"rooftop", "point", "range_interpolation"}


class GeocodingError(ValueError):
    """Provider response is not safe to persist as a location."""


def address_query(address: dict[str, Any]) -> str:
    parts = [address.get("line1"), address.get("line2"), ", ".join(filter(None, [address.get("city"), address.get("state")])), address.get("postal_code"), address.get("country")]
    return ", ".join(str(part) for part in parts if part)


def validate_geocodio_result(result: dict[str, Any], address: dict[str, Any]) -> dict[str, Any]:
    location = result.get("location") or {}
    try:
        latitude, longitude, accuracy = float(location["lat"]), float(location["lng"]), float(result["accuracy"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GeocodingError("Geocodio result lacks numeric coordinates or accuracy") from exc
    if not all(math.isfinite(value) for value in (latitude, longitude, accuracy)) or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise GeocodingError("Geocodio returned invalid WGS84 coordinates")
    if accuracy < MIN_ACCURACY or result.get("accuracy_type") not in ADDRESS_LEVEL_TYPES:
        raise GeocodingError("Geocodio result is not a sufficiently precise address-level match")
    components = result.get("address_components") or {}
    actual = {"number": components.get("number"), "city": components.get("city"), "state": components.get("state_province", components.get("state")), "postal_code": components.get("postal_code", components.get("zip")), "country": components.get("country")}
    expected = {"number": address["line1"].split()[0], "city": address["city"], "state": address["state"], "postal_code": address["postal_code"], "country": address["country"]}
    if any(str(actual[key]).strip().upper() != str(value).strip().upper() for key, value in expected.items()):
        raise GeocodingError("Geocodio standardized address does not match the submitted street address")
    return {"type": "Point", "coordinates": [longitude, latitude]}


async def geocode_canonical_address(address: dict[str, Any], api_key: str, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Forward-geocode once for onboarding; runtime operations use persisted GeoJSON."""
    if not api_key:
        raise GeocodingError("GEOCODIO_API_KEY is required")
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        response = await client.get(GEOCODIO_FORWARD_URL, params={"q": address_query(address), "country": address["country"], "api_key": api_key})
        response.raise_for_status()
        results = response.json().get("results") or []
        if not results:
            raise GeocodingError("Geocodio returned no results")
        return validate_geocodio_result(results[0], address)
    except httpx.HTTPError as exc:
        raise GeocodingError("Geocodio request failed") from exc
    finally:
        if owns_client:
            await client.aclose()
