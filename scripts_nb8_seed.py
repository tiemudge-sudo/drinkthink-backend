"""One-time NB-8 canonical organization/location onboarding migration.

Run only after replacing the placeholder coordinates with the verified geocoder result.
The migration is idempotent and does not touch frontend seed files.
"""
import asyncio
import os
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient

ORG_ID = "org_ties_house"
LOCATION_ID = "loc_ties_house"

# Canonical source address from frontend test-locations.ts.
ADDRESS = {
    "line1": "3417 S. Almeria Ave",
    "line2": None,
    "city": "Tampa",
    "state": "FL",
    "postal_code": "33629",
    "country": "US",
}

# Set from a verified onboarding geocoder before running this migration.
LATITUDE = os.environ.get("NB8_TIES_HOUSE_LATITUDE")
LONGITUDE = os.environ.get("NB8_TIES_HOUSE_LONGITUDE")

async def main():
    if LATITUDE is None or LONGITUDE is None:
        raise SystemExit("Set NB8_TIES_HOUSE_LATITUDE and NB8_TIES_HOUSE_LONGITUDE from the verified geocoder result")
    latitude, longitude = float(LATITUDE), float(LONGITUDE)
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise SystemExit("Invalid WGS84 coordinates")

    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    now = datetime.now(timezone.utc)
    try:
        await db.organizations.update_one(
            {"organization_id": ORG_ID},
            {"$setOnInsert": {"organization_id": ORG_ID, "created_at": now},
             "$set": {"name": "Tie's House", "type": "restaurant", "status": "active", "updated_at": now}},
            upsert=True,
        )
        await db.locations.update_one(
            {"location_id": LOCATION_ID},
            {"$setOnInsert": {"location_id": LOCATION_ID, "created_at": now},
             "$set": {
                 "organization_id": ORG_ID,
                 "name": "Tie's house",
                 "address": ADDRESS,
                 "geo": {"type": "Point", "coordinates": [longitude, latitude]},
                 "timezone": "America/New_York",
                 "status": "active",
                 "pos_connection_id": None,
                 "updated_at": now,
             }},
            upsert=True,
        )
        print({"organization_id": ORG_ID, "location_id": LOCATION_ID, "geo": {"type": "Point", "coordinates": ["<longitude>", "<latitude>"]}})
    finally:
        client.close()

if __name__ == "__main__":
    asyncio.run(main())
