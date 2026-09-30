"""Idempotent NB-8 canonical Tie's House onboarding migration for Railway."""
import asyncio
import os
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient
from location_geocoding import geocode_canonical_address

EXPECTED_DB_NAME = "DrinkThinkV0"
ORG_ID = "org_ties_house"
LOCATION_ID = "loc_ties_house"
CHECK_IN_CONFIG_ID = "location_check_in"
CHECK_IN_RADIUS_FEET = 200
ADDRESS = {"line1": "3417 S. Almeria Ave", "line2": None, "city": "Tampa", "state": "FL", "postal_code": "33629", "country": "US"}


async def validate(db, latitude: float, longitude: float) -> dict:
    org = await db.organizations.find_one({"organization_id": ORG_ID}, {"_id": 0})
    location = await db.locations.find_one({"location_id": LOCATION_ID}, {"_id": 0})
    config = await db.app_config.find_one({"_id": CHECK_IN_CONFIG_ID}, {"_id": 0})
    location_indexes = {item["name"] for item in await db.locations.list_indexes().to_list(None)}
    organization_indexes = {item["name"] for item in await db.organizations.list_indexes().to_list(None)}
    expected_geo = {"type": "Point", "coordinates": [longitude, latitude]}
    failures = []
    if not org or any(org.get(k) != v for k, v in {"organization_id": ORG_ID, "name": "Tie's House", "type": "restaurant", "status": "active"}.items()):
        failures.append("organization record is missing or invalid")
    expected_location = {"location_id": LOCATION_ID, "organization_id": ORG_ID, "name": "Tie's house", "address": ADDRESS, "timezone": "America/New_York", "status": "active"}
    if not location or any(location.get(k) != v for k, v in expected_location.items()):
        failures.append("location record is missing or invalid")
    if not location or location.get("geo") != expected_geo:
        failures.append("location GeoJSON is missing, invalid, or not [longitude, latitude]")
    if location and any(k in location for k in ("is_test", "check_in_radius_meters", "latitude", "longitude")):
        failures.append("location contains prohibited test, radius, or duplicate-coordinate fields")
    if not config or config.get("check_in_radius_feet") != CHECK_IN_RADIUS_FEET:
        failures.append("global check-in radius is missing or is not 200 feet")
    required_location_indexes = {"location_id_1", "organization_id_1", "status_1", "geo_2dsphere"}
    if not required_location_indexes.issubset(location_indexes):
        failures.append("required locations indexes are missing")
    if "organization_id_1" not in organization_indexes:
        failures.append("required organizations organization_id index is missing")
    if failures:
        raise RuntimeError("Post-migration validation failed: " + "; ".join(failures))
    return {"database": EXPECTED_DB_NAME, "organization_id": ORG_ID, "location_id": LOCATION_ID, "geo": expected_geo, "check_in_radius_feet": 200, "check_in_radius_meters": 60.96, "locations_indexes": sorted(required_location_indexes), "organizations_indexes": ["organization_id_1"]}


async def main():
    db_name = os.environ.get("DB_NAME")
    if db_name != EXPECTED_DB_NAME:
        raise RuntimeError(f"Safety check failed: DB_NAME must be {EXPECTED_DB_NAME}")
    mongo_url = os.environ.get("MONGO_URL")
    if not mongo_url:
        raise RuntimeError("Missing required environment variable: MONGO_URL")
    api_key = os.environ.get("GEOCODIO_API_KEY")
    if not api_key:
        raise RuntimeError("Missing required environment variable: GEOCODIO_API_KEY")
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]
    now = datetime.now(timezone.utc)
    try:
        existing = await db.locations.find_one({"location_id": LOCATION_ID}, {"_id": 0, "address": 1, "geo": 1})
        existing_coordinates = (existing or {}).get("geo", {}).get("coordinates", [])
        valid_existing_geo = len(existing_coordinates) == 2 and all(isinstance(value, (int, float)) for value in existing_coordinates) and -180 <= existing_coordinates[0] <= 180 and -90 <= existing_coordinates[1] <= 90
        if existing and existing.get("address") == ADDRESS and existing.get("geo", {}).get("type") == "Point" and valid_existing_geo:
            geo = existing["geo"]
        else:
            geo = await geocode_canonical_address(ADDRESS, api_key)
        longitude, latitude = geo["coordinates"]
        await db.organizations.create_index("organization_id", unique=True)
        await db.locations.create_index("location_id", unique=True)
        await db.locations.create_index("organization_id")
        await db.locations.create_index("status")
        await db.locations.create_index([("geo", "2dsphere")])
        await db.organizations.update_one({"organization_id": ORG_ID}, {"$setOnInsert": {"organization_id": ORG_ID, "created_at": now}, "$set": {"name": "Tie's House", "type": "restaurant", "status": "active", "updated_at": now}}, upsert=True)
        await db.locations.update_one({"location_id": LOCATION_ID}, {"$setOnInsert": {"location_id": LOCATION_ID, "created_at": now}, "$set": {"organization_id": ORG_ID, "name": "Tie's house", "address": ADDRESS, "geo": geo, "timezone": "America/New_York", "status": "active", "pos_connection_id": None, "updated_at": now}, "$unset": {"is_test": "", "check_in_radius_meters": "", "latitude": "", "longitude": ""}}, upsert=True)
        await db.app_config.update_one({"_id": CHECK_IN_CONFIG_ID}, {"$setOnInsert": {"created_at": now}, "$set": {"check_in_radius_feet": CHECK_IN_RADIUS_FEET, "updated_at": now}}, upsert=True)
        print({"nb8_migration": "success", **await validate(db, latitude, longitude)})
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
