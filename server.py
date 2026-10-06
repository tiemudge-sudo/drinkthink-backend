from fastapi import FastAPI, APIRouter, HTTPException, Header, Depends, Request, Response
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError, DuplicateKeyError
from bson import json_util
import os
import json
import uuid
import logging
import asyncio
import base64
import hashlib
import hmac
import secrets
import re
import math
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import BaseModel, ConfigDict, Field, EmailStr, field_validator, model_validator
from typing import Any, List, Optional, Literal

import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken

from ingredient_resolution import (
    IngredientResolutionError,
    parse_ingredient_id,
    resolve_ingredient_id_map,
    resolve_ingredient_ids,
)
from location_geocoding import GeocodingError, geocode_canonical_address


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

app = FastAPI()
api_router = APIRouter(prefix="/api")


# This client is intentionally separate from the application client above.  It
# is used only by the narrowly-scoped, read-only automation API.  In Railway,
# DRINKTHINK_AI_MONGO_URL must be a MongoDB user without write privileges.
_ai_mongo_url = os.environ.get("DRINKTHINK_AI_MONGO_URL")
ai_client = AsyncIOMotorClient(_ai_mongo_url) if _ai_mongo_url else None
ai_db = ai_client[os.environ.get("DRINKTHINK_AI_DB_NAME", os.environ["DB_NAME"])] if ai_client else None

# Only canonical Master Data resources are available to automation.  This is
# deliberately not derived from list_collection_names(): new/private database
# collections never become available merely by being created.
AI_READ_COLLECTIONS = {
    "cocktails": {
        "record_id": "cocktail_id",
        "filter_fields": {"cocktail_id": str, "status": str, "category": str,
                          "alcohol_class": str, "glass_id": str, "source": str,
                          "legacy_drink_id": int},
    },
    "ingredients": {
        "record_id": "ingredient_id",
        "filter_fields": {"ingredient_id": int, "status": str, "primary_category": str,
                          "primary_ingredient": str},
    },
    "ingredient_categories": {
        "record_id": "category_id",
        "filter_fields": {"category_id": str, "status": str, "name": str},
    },
    "glasses": {
        "record_id": "glass_id",
        "filter_fields": {"glass_id": str, "status": str, "name": str},
    },
    "cocktail_ingredients": {
        "record_id": None,
        "filter_fields": {"cocktail_id": str, "ingredient_id": int, "required": bool},
    },
}
AI_MAX_PAGE_SIZE = 100
AI_SCHEMA_SAMPLE_SIZE = 25


def _ai_db_or_503():
    if ai_db is None:
        raise HTTPException(status_code=503, detail="AI read-only database is not configured")
    return ai_db


def _ai_authorized(authorization: Optional[str] = Header(None)) -> None:
    """Require the dedicated automation secret; never use a consumer session."""
    expected = os.environ.get("DRINKTHINK_AI_API_KEY")
    if not expected:
        raise HTTPException(status_code=503, detail="AI API is not configured")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    supplied = authorization.split(" ", 1)[1].strip()
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Invalid bearer token")


def _ai_collection_or_404(collection: str) -> dict:
    config = AI_READ_COLLECTIONS.get(collection)
    if not config:
        raise HTTPException(status_code=404, detail="Collection is not available to the AI API")
    return config


def _ai_parse_filter(collection_config: dict, field: Optional[str], value: Optional[str]) -> dict:
    """Construct one exact-match filter from an explicit field allowlist.

    The API intentionally does not accept BSON/JSON filters or MongoDB
    operators, avoiding NoSQL injection and unbounded database queries.
    """
    if field is None and value is None:
        return {}
    if not field or value is None:
        raise HTTPException(status_code=400, detail="filter_field and filter_value must be supplied together")
    value_type = collection_config["filter_fields"].get(field)
    if value_type is None:
        raise HTTPException(status_code=400, detail="filter field is not allowed")
    try:
        if value_type is bool:
            if value.lower() not in {"true", "false"}:
                raise ValueError()
            parsed = value.lower() == "true"
        else:
            parsed = value_type(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="filter value has an invalid type")
    return {field: parsed}


def _ai_json_response(value) -> JSONResponse:
    """Return canonical MongoDB Extended JSON, including ObjectId and dates."""
    encoded = json.loads(json_util.dumps(value, json_options=json_util.CANONICAL_JSON_OPTIONS))
    return JSONResponse(content=encoded)


def _ai_shape(value):
    if isinstance(value, dict):
        return {key: _ai_shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return {"type": "array", "items": _ai_shape(value[0]) if value else "unknown"}
    return type(value).__name__


# ---------------- AI read-only database API ----------------
@api_router.get("/ai/health", dependencies=[Depends(_ai_authorized)])
async def ai_health():
    database = _ai_db_or_503()
    try:
        await database.command({"ping": 1})
    except Exception:
        logger.exception("AI read-only database health check failed")
        raise HTTPException(status_code=503, detail="AI read-only database is unavailable")
    return {"status": "ok", "database": database.name, "read_only": True}


@api_router.get("/ai/collections", dependencies=[Depends(_ai_authorized)])
async def ai_collections():
    _ai_db_or_503()
    return {"collections": [
        {"name": name, "record_id": config["record_id"],
         "filter_fields": sorted(config["filter_fields"])}
        for name, config in AI_READ_COLLECTIONS.items()
    ]}


@api_router.get("/ai/collections/{collection}/count", dependencies=[Depends(_ai_authorized)])
async def ai_count(collection: str, filter_field: Optional[str] = None, filter_value: Optional[str] = None):
    config = _ai_collection_or_404(collection)
    query = _ai_parse_filter(config, filter_field, filter_value)
    count = await _ai_db_or_503()[collection].count_documents(query)
    return {"collection": collection, "filter": query, "count": count}


@api_router.get("/ai/collections/{collection}/schema", dependencies=[Depends(_ai_authorized)])
async def ai_schema(collection: str):
    _ai_collection_or_404(collection)
    docs = await _ai_db_or_503()[collection].find({}).limit(AI_SCHEMA_SAMPLE_SIZE).to_list(AI_SCHEMA_SAMPLE_SIZE)
    fields = {}
    for doc in docs:
        for name, value in doc.items():
            fields.setdefault(name, _ai_shape(value))
    return {"collection": collection, "sample_size": len(docs), "fields": fields}


@api_router.get("/ai/collections/{collection}/{record_id}", dependencies=[Depends(_ai_authorized)])
async def ai_record(collection: str, record_id: str):
    config = _ai_collection_or_404(collection)
    id_field = config["record_id"]
    if not id_field:
        raise HTTPException(status_code=400, detail="This collection has no supported single record identifier")
    query = _ai_parse_filter(config, id_field, record_id)
    doc = await _ai_db_or_503()[collection].find_one(query)
    if not doc:
        raise HTTPException(status_code=404, detail="Record not found")
    return _ai_json_response(doc)


@api_router.get("/ai/collections/{collection}", dependencies=[Depends(_ai_authorized)])
async def ai_records(
    collection: str,
    offset: int = 0,
    limit: int = 25,
    filter_field: Optional[str] = None,
    filter_value: Optional[str] = None,
):
    config = _ai_collection_or_404(collection)
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be non-negative")
    if limit < 1 or limit > AI_MAX_PAGE_SIZE:
        raise HTTPException(status_code=400, detail=f"limit must be between 1 and {AI_MAX_PAGE_SIZE}")
    query = _ai_parse_filter(config, filter_field, filter_value)
    sort_field = config["record_id"] or "cocktail_id"
    docs = await _ai_db_or_503()[collection].find(query).sort(sort_field, 1).skip(offset).limit(limit).to_list(limit)
    return _ai_json_response({"collection": collection, "filter": query, "offset": offset,
                              "limit": limit, "records": docs})


# ------------- Models -------------
class Drink(BaseModel):
    # Existing mobile response shape retained; id now carries canonical cocktail_id.
    id: str
    name: str
    category: str = ""
    alcohol: str = ""
    glass: str = ""
    glass_category_id: str = ""
    ingredients: str = ""
    instructions: str = ""
    shopping: str = ""
    fancy: float
    dark: float
    thirsty: float
    calm: float
    celebrate: float


def _canonical_to_drink(doc: dict, glass: Optional[dict] = None) -> dict:
    """Adapt canonical Mongo cocktail documents to the existing mobile Drink payload."""
    scores = doc.get("scores") or {}
    migration = doc.get("migration") or {}
    return {
        "id": doc["cocktail_id"],
        "name": doc.get("name") or "",
        "category": doc.get("category") or "",
        "alcohol": doc.get("alcohol_class") or "",
        # Drink Style filtering remains glass-owned. Card icon identity is
        # instead the cocktail's canonical glass category.
        "glass": (glass or {}).get("display_name") or (glass or {}).get("name") or doc.get("glass_id") or "",
        "glass_category_id": doc.get("glass_category_id") or "",
        "ingredients": doc.get("human_ingredients") or "",
        "instructions": doc.get("instructions") or "",
        "shopping": doc.get("shopping_tokens") or "",
        "fancy": float(scores.get("fancy") or 0),
        "dark": float(scores.get("strong") or 0),
        "thirsty": float(scores.get("thirsty") or 0),
        "calm": float(scores.get("comfort") or 0),
        "celebrate": float(scores.get("party") or 0),
    }


async def _canonical_glasses_by_id() -> dict[str, dict]:
    """Return canonical glass metadata keyed by glass_id for response shaping."""
    glass_docs = await db.glasses.find({"status": "active"}, {"_id": 0}).to_list(length=None)
    return {str(g["glass_id"]): g for g in glass_docs if g.get("glass_id")}


def _canonical_read_model_ttl_seconds() -> float:
    """Return a bounded, Railway-configurable TTL without failing startup."""
    try:
        return max(1.0, min(float(os.environ.get("CANONICAL_READ_MODEL_CACHE_TTL_SECONDS", "60")), 3600.0))
    except ValueError:
        return 60.0


@dataclass(frozen=True)
class CanonicalReadModel:
    """Immutable canonical source data used by recommendation and Shake paths."""
    cocktails: tuple[dict, ...]
    ingredients_by_id: dict[int, dict]
    active_ingredient_ids: frozenset[int]
    glasses_by_id: dict[str, dict]
    active_merge_map: dict[int, int]
    # Required recipe ingredients are canonicalized when the bounded source
    # snapshot refreshes. ``None`` preserves the existing meaning of a stale
    # or invalid historical relationship: that cocktail is not cupboard-ready.
    required_ingredients_by_cocktail: dict[str, tuple[Optional[int], ...]]


class RequestTiming:
    """Request-local, non-sensitive duration collector for Server-Timing."""
    def __init__(self):
        self.started = time.perf_counter()
        self.phases: dict[str, float] = {}

    @contextmanager
    def phase(self, name: str):
        started = time.perf_counter()
        try:
            yield
        finally:
            self.phases[name] = self.phases.get(name, 0.0) + (time.perf_counter() - started) * 1000

    def add(self, name: str, duration_ms: float) -> None:
        self.phases[name] = self.phases.get(name, 0.0) + duration_ms

    def set_response_header(self, response: Optional[Response], cache_state: str) -> None:
        if response is None:
            return
        total_ms = (time.perf_counter() - self.started) * 1000
        entries = [f"cache;desc=\"{cache_state}\";dur=0"]
        entries.extend(f"{name};dur={duration:.1f}" for name, duration in self.phases.items())
        entries.append(f"total;dur={total_ms:.1f}")
        response.headers["Server-Timing"] = ", ".join(entries)


class CanonicalReadModelCache:
    """One bounded snapshot per process; no user or request result data is cached."""
    def __init__(self):
        self._snapshot: Optional[CanonicalReadModel] = None
        self._expires_at = 0.0
        self._source_db_id: Optional[int] = None
        self._lock = asyncio.Lock()

    def clear(self) -> None:
        self._snapshot = None
        self._expires_at = 0.0
        self._source_db_id = None

    async def get(self, timing: RequestTiming) -> tuple[CanonicalReadModel, str]:
        now = time.monotonic()
        if self._snapshot and self._source_db_id == id(db) and now < self._expires_at:
            return self._snapshot, "hit"

        async with self._lock:
            now = time.monotonic()
            if self._snapshot and self._source_db_id == id(db) and now < self._expires_at:
                return self._snapshot, "hit_after_wait"

            async def load(name: str, cursor) -> list[dict]:
                started = time.perf_counter()
                try:
                    return await cursor.to_list(length=None)
                finally:
                    timing.add(name, (time.perf_counter() - started) * 1000)

            # These independent reads are deliberately concurrent. A snapshot is
            # assigned only after every canonical source loaded successfully.
            cocktails, ingredients, glasses, merges = await asyncio.gather(
                load("cocktails", db.cocktails.find({"status": "active"}, {"_id": 0})),
                load("ingredients", db.ingredients.find({"status": "active"}, {"_id": 0})),
                load("glasses", db.glasses.find({"status": "active"}, {"_id": 0})),
                load("ingredient_merges", db.ingredient_id_merges.find(
                    {"status": "active"}, {"_id": 0, "from_id": 1, "resolved_to_id": 1}
                )),
            )
            ingredients_by_id = {
                int(row["ingredient_id"]): row
                for row in ingredients
                if isinstance(row.get("ingredient_id"), int)
            }
            merge_map: dict[int, int] = {}
            for row in merges:
                try:
                    merge_map[parse_ingredient_id(row.get("from_id"))] = parse_ingredient_id(row.get("resolved_to_id"))
                except IngredientResolutionError:
                    # Invalid source data is handled by the existing resolver on
                    # request input; it must not make a successful snapshot lie.
                    continue
            active_cocktail_ids = [
                str(row["cocktail_id"])
                for row in cocktails
                if row.get("cocktail_id")
            ]
            recipe_rows = await load(
                "recipe_requirements",
                db.cocktail_ingredients.find(
                    {"cocktail_id": {"$in": active_cocktail_ids}, "required": True},
                    {"_id": 0, "cocktail_id": 1, "ingredient_id": 1},
                ),
            )
            with timing.phase("recipe_ingredient_normalization"):
                recipe_id_map = await resolve_ingredient_id_map(
                    db,
                    (row.get("ingredient_id") for row in recipe_rows),
                    strict=False,
                    active_merge_map=merge_map,
                    active_ingredient_ids=frozenset(ingredients_by_id),
                )
                required_ingredients: dict[str, list[Optional[int]]] = {}
                for row in recipe_rows:
                    cocktail_id = row.get("cocktail_id")
                    if not cocktail_id:
                        continue
                    try:
                        source_id = parse_ingredient_id(row.get("ingredient_id"))
                    except IngredientResolutionError:
                        canonical_id = None
                    else:
                        canonical_id = recipe_id_map.get(source_id)
                    required_ingredients.setdefault(str(cocktail_id), []).append(canonical_id)
            snapshot = CanonicalReadModel(
                cocktails=tuple(cocktails),
                ingredients_by_id=ingredients_by_id,
                active_ingredient_ids=frozenset(ingredients_by_id),
                glasses_by_id={str(row["glass_id"]): row for row in glasses if row.get("glass_id")},
                active_merge_map=merge_map,
                required_ingredients_by_cocktail={
                    cocktail_id: tuple(ingredient_ids)
                    for cocktail_id, ingredient_ids in required_ingredients.items()
                },
            )
            self._snapshot = snapshot
            self._source_db_id = id(db)
            self._expires_at = time.monotonic() + _canonical_read_model_ttl_seconds()
            return snapshot, "refresh"


_canonical_read_model_cache = CanonicalReadModelCache()


class MatchItem(BaseModel):
    drink: Drink
    score: float
    is_favorite: bool = False


class MatchResponse(BaseModel):
    results: List[MatchItem]
    scoring: str
    query: dict         # user slider labels -> values
    query_mapped: dict  # mapped to DB dims


class ShakeShotResponse(BaseModel):
    """One randomly selected, already-eligible Shot drink, if any."""
    drink: Optional[Drink] = None



# ------------- Public Hospitality Partner Applications -------------

class HospitalityPartnerApplicationRequest(BaseModel):
    business_name: str = Field(min_length=2, max_length=160)
    location_name: Optional[str] = Field(default=None, max_length=160)
    street_address: str = Field(min_length=3, max_length=200)
    city: str = Field(min_length=2, max_length=100)
    state_region: str = Field(min_length=2, max_length=100)
    postal_code: str = Field(min_length=2, max_length=20)
    country: str = Field(default="US", min_length=2, max_length=2)
    website_url: Optional[str] = Field(default=None, max_length=500)
    menu_url: Optional[str] = Field(default=None, max_length=500)
    contact_title_role: Optional[str] = Field(default=None, max_length=120)

    contact_name: str = Field(min_length=2, max_length=120)
    contact_email: EmailStr
    contact_phone: Optional[str] = Field(default=None, max_length=32)

    has_multiple_locations: bool
    partnership_path: Literal["ordering", "drink_menu", "general_interest"]
    pos_provider: Optional[str] = Field(default=None, max_length=80)
    comments: Optional[str] = Field(default=None, max_length=2000)

    @field_validator(
    "business_name",
    "location_name",
    "street_address",
    "city",
    "state_region",
    "postal_code",
    "website_url",
    "menu_url",
    "contact_title_role",
    "contact_name",
    "contact_phone",
    "pos_provider",
    "comments",
    mode="before"
    )
    @classmethod
    def normalize_text(cls, value):
        if value is None:
            return None
        value = re.sub(r"\s+", " ", str(value)).strip()
        return value or None

    @field_validator("country")
    @classmethod
    def normalize_country(cls, value: str):
        return value.strip().upper()

    @field_validator("contact_email", mode="before")
    @classmethod
    def normalize_email(cls, value):
        return str(value).strip().lower()

    @model_validator(mode="after")
    def validate_path(self):
        if self.partnership_path == "ordering" and not self.pos_provider:
            raise ValueError("pos_provider is required when partnership_path is 'ordering'")
        return self


class HospitalityPartnerApplicationResponse(BaseModel):
    application_id: str
    status: Literal["pending"]
    submitted_at: datetime


def _partner_application_id() -> str:
    return "hpa_" + secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16]


async def _send_partner_application_notification(document: dict) -> None:
    """Send the administrative partner-application email after persistence.

    Notification delivery is deliberately non-authoritative: callers must catch
    failures so a stored application still returns the normal 201 response.
    """
    api_key = os.environ.get("RESEND_API_KEY")
    recipient = os.environ.get("PARTNER_NOTIFICATION_EMAIL")
    sender = os.environ.get("PARTNER_NOTIFICATION_FROM")
    if not api_key or not recipient or not sender:
        raise RuntimeError("Partner notification email is not fully configured")

    def show(value) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "Yes" if value else "No"
        return str(value)

    location_name = document.get("location_name")
    subject = f"New Hospitality Partner Application — {document['business_name']}"
    if location_name:
        subject += f" / {location_name}"

    address_parts = [
        document.get("street_address"),
        document.get("city"),
        document.get("state_region"),
        document.get("postal_code"),
        document.get("country"),
    ]
    full_address = ", ".join(show(part) for part in address_parts if part)

    fields = [
        ("Application ID", document.get("application_id")),
        ("Submitted", document.get("submitted_at").isoformat() if isinstance(document.get("submitted_at"), datetime) else document.get("submitted_at")),
        ("Business name", document.get("business_name")),
        ("Location name", location_name),
        ("Full address", full_address),
        ("Website URL", document.get("website_url")),
        ("Menu URL", document.get("menu_url")),
        ("Multiple locations", document.get("has_multiple_locations")),
        ("Contact name", document.get("contact_name")),
        ("Contact title/role", document.get("contact_title_role")),
        ("Contact email", document.get("contact_email")),
        ("Contact phone", document.get("contact_phone")),
        ("Partnership path", document.get("partnership_path")),
        ("POS provider", document.get("pos_provider")),
        ("Comments", document.get("comments")),
    ]
    text_body = "\n".join(f"{label}: {show(value)}" for label, value in fields if value is not None and value != "")

    async with httpx.AsyncClient(timeout=10.0) as client_http:
        response = await client_http.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": sender,
                "to": [recipient],
                "subject": subject,
                "text": text_body,
            },
        )
        response.raise_for_status()


def _client_ip(request: Request) -> str:
    # Railway/Cloudflare deployments normally supply X-Forwarded-For.
    # Only the first address is used for abuse throttling and it is not persisted.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


# Per-process burst guard. Production edge rate limiting should also be enabled
# at Cloudflare/Railway because this process-local guard does not coordinate
# across multiple workers/instances.
_PARTNER_RATE_WINDOW_SECONDS = 3600
_PARTNER_RATE_MAX = 5
_partner_rate_events: dict[str, list[float]] = {}


def _enforce_partner_rate_limit(request: Request, response: Response) -> None:
    now = datetime.now(timezone.utc).timestamp()
    key = _client_ip(request)
    cutoff = now - _PARTNER_RATE_WINDOW_SECONDS
    events = [ts for ts in _partner_rate_events.get(key, []) if ts > cutoff]
    if len(events) >= _PARTNER_RATE_MAX:
        retry_after = max(1, int(events[0] + _PARTNER_RATE_WINDOW_SECONDS - now))
        raise HTTPException(
            status_code=429,
            detail={
                "code": "rate_limit_exceeded",
                "message": "Too many partner applications. Please try again later.",
            },
            headers={"Retry-After": str(retry_after)},
        )
    events.append(now)
    _partner_rate_events[key] = events
    response.headers["X-RateLimit-Limit"] = str(_PARTNER_RATE_MAX)
    response.headers["X-RateLimit-Remaining"] = str(max(0, _PARTNER_RATE_MAX - len(events)))


@api_router.post(
    "/public/hospitality-partner-applications",
    response_model=HospitalityPartnerApplicationResponse,
    status_code=201,
)
async def create_hospitality_partner_application(
    body: HospitalityPartnerApplicationRequest,
    request: Request,
    response: Response,
):
    """Accept a prospective hospitality partner application.

    This endpoint is intentionally unauthenticated. It creates only a pending
    application record. It MUST NOT provision an organization, location,
    vendor account, POS connection, or operational configuration.
    """
    _enforce_partner_rate_limit(request, response)

    now = datetime.now(timezone.utc)
    application_id = _partner_application_id()

    document = {
        "_id": application_id,
        "application_id": application_id,
        "status": "pending",
        "submitted_at": now,
        "updated_at": now,
        **body.model_dump(mode="json"),
    }

    await db.hospitality_partner_applications.insert_one(document)

    # Persistence is authoritative. Notification failure must never convert a
    # successfully stored application into a failed public submission.
    try:
        await _send_partner_application_notification(document)
        logger.info("Partner application notification sent application_id=%s", application_id)
    except Exception:
        logger.exception("Partner application notification failed application_id=%s", application_id)

    return HospitalityPartnerApplicationResponse(
        application_id=application_id,
        status="pending",
        submitted_at=now,
    )

# ------------- Slider → DB dimension map -------------
# User labels: Strong, Fancy, Comfort, Party, Thirsty
# DB dims:     Dark,   Fancy, Calm,    Celebrate, Thirsty
SLIDER_TO_DIM = {
    "strong": "dark",
    "fancy": "fancy",
    "comfort": "calm",
    "party": "celebrate",
    "thirsty": "thirsty",
}
DIMS = ["fancy", "dark", "thirsty", "calm", "celebrate"]


# ------------- Routes -------------
@api_router.get("/")
async def root():
    return {"message": "Drink Think API", "drinks": await db.cocktails.count_documents({"status": "active"})}


@api_router.get("/drinks/count")
async def drinks_count():
    return {"count": await db.cocktails.count_documents({"status": "active"})}


MAIN_INGREDIENT_PRIMARY = {
    "vodka": "vodka",
    "gin": "gin",
    "rum": "rum",
    "whiskey": "whiskey",
    "tequila": "tequila",
}
ALLOWED_ALCOHOL_FILTERS = set(MAIN_INGREDIENT_PRIMARY) | {"non_alcoholic"}

# Consumer Drink Style values.  Their membership is owned by
# glasses.filter_families, never by a frontend or backend lookup table.
ALLOWED_GLASS_FILTERS = {"shot", "rocks", "pint", "martini", "hurricane", "champagne_flutes"}


def _int_ids(values) -> set[int]:
    out: set[int] = set()
    for value in values or []:
        try:
            out.add(int(value))
        except (TypeError, ValueError):
            continue
    return out


def _ingredient_signature(ingredient: dict) -> tuple[str, str]:
    return (
        str(ingredient.get("primary_category") or "").strip().lower(),
        str(ingredient.get("primary_ingredient") or "").strip().lower(),
    )


async def _resolved_historical_ids(values, read_model: Optional[CanonicalReadModel] = None) -> list[int]:
    """Resolve persisted IDs without turning one stale record into a 500."""
    return await resolve_ingredient_ids(
        db,
        values or [],
        strict=False,
        active_merge_map=read_model.active_merge_map if read_model else None,
        active_ingredient_ids=read_model.active_ingredient_ids if read_model else None,
    )


async def _resolved_id_map(values, read_model: Optional[CanonicalReadModel] = None) -> dict[int, int]:
    """Resolve persisted references in two bounded queries."""
    return await resolve_ingredient_id_map(
        db,
        values or [],
        strict=False,
        active_merge_map=read_model.active_merge_map if read_model else None,
        active_ingredient_ids=read_model.active_ingredient_ids if read_model else None,
    )


async def _canonical_recipe_requirements(
    cocktail_ids: list[str], read_model: Optional[CanonicalReadModel] = None
) -> dict[str, list[Optional[int]]]:
    """Load and resolve recipe IDs; unresolved persisted values fail capability checks."""
    if read_model is not None:
        return {
            cocktail_id: list(read_model.required_ingredients_by_cocktail[cocktail_id])
            for cocktail_id in cocktail_ids
            if cocktail_id in read_model.required_ingredients_by_cocktail
        }
    rows = await db.cocktail_ingredients.find(
        {"cocktail_id": {"$in": cocktail_ids}, "required": True},
        {"_id": 0, "cocktail_id": 1, "ingredient_id": 1},
    ).to_list(length=None)
    id_map = await _resolved_id_map((row.get("ingredient_id") for row in rows), read_model)
    requirements: dict[str, list[Optional[int]]] = {}
    for row in rows:
        try:
            source_id = parse_ingredient_id(row.get("ingredient_id"))
        except IngredientResolutionError:
            canonical_id = None
        else:
            canonical_id = id_map.get(source_id)
        requirements.setdefault(row["cocktail_id"], []).append(canonical_id)
    return requirements


def _inventory_satisfies_requirements(
    required_ids: list[Optional[int]], available_ids: set[int], ingredients_by_id: dict[int, dict]
) -> tuple[bool, list[int], list[int], bool]:
    """Use the same exact-or-primary-ingredient semantics as My Cupboard."""
    signatures = {
        _ingredient_signature(ingredients_by_id[item_id])
        for item_id in available_ids if item_id in ingredients_by_id
    }
    matched, missing = [], []
    has_unresolved_requirement = False
    for ingredient_id in required_ids:
        if ingredient_id is None:
            has_unresolved_requirement = True
        elif ingredient_id in available_ids or (
            ingredient_id in ingredients_by_id and _ingredient_signature(ingredients_by_id[ingredient_id]) in signatures
        ):
            matched.append(ingredient_id)
        else:
            missing.append(ingredient_id)
    return not missing and not has_unresolved_requirement, matched, missing, has_unresolved_requirement


CAPABILITY_INGREDIENT_CATEGORIES = frozenset({
    "primary_liquor", "secondary_liquor", "wine", "beer", "mixer", "common_items",
})


def _location_category_is_assumed_available(category_id: str, settings: Optional[dict]) -> bool:
    """Apply the locked location-only capability policy; never use this for cupboards."""
    if category_id == "common_items":
        return True
    settings = settings or {}
    if category_id == "secondary_liquor":
        return settings.get("secondary_liquor_availability") == "assumed_available"
    if category_id == "mixer":
        return settings.get("mixer_availability") == "assumed_available"
    return False


def _location_inventory_satisfies_requirements(
    required_ids: list[Optional[int]],
    available_ids: set[int],
    ingredients_by_id: dict[int, dict],
    settings: Optional[dict],
) -> tuple[bool, list[int], list[int], bool]:
    """Evaluate derived location capability from evidence and locked availability policy."""
    signatures = {
        _ingredient_signature(ingredients_by_id[item_id])
        for item_id in available_ids if item_id in ingredients_by_id
    }
    matched, missing = [], []
    has_unresolved_requirement = False
    for ingredient_id in required_ids:
        if ingredient_id is None:
            has_unresolved_requirement = True
            continue
        ingredient = ingredients_by_id.get(ingredient_id)
        category_id = str((ingredient or {}).get("category_id") or "").strip()
        if category_id not in CAPABILITY_INGREDIENT_CATEGORIES:
            has_unresolved_requirement = True
            continue
        if _location_category_is_assumed_available(category_id, settings):
            # Assumption is capability policy, not physical inventory evidence.
            continue
        if ingredient_id in available_ids or _ingredient_signature(ingredient) in signatures:
            matched.append(ingredient_id)
        else:
            missing.append(ingredient_id)
    return not missing and not has_unresolved_requirement, matched, missing, has_unresolved_requirement


async def resolve_effective_location_inventory(
    location_id: str, read_model: CanonicalReadModel, settings: Optional[dict] = None
) -> set[int]:
    """Resolve configured inventory without ever consulting the visiting user."""
    if settings is None:
        settings = await db.location_settings.find_one({"location_id": location_id}, {"_id": 0, "inventory_source": 1})
    source = (settings or {}).get("inventory_source") or {}
    if source.get("kind") == "user_cupboard":
        owner_id = source.get("user_id")
        if not isinstance(owner_id, str) or not owner_id:
            return set()
        cupboard = await db.user_cupboard.find_one({"user_id": owner_id}, {"_id": 0, "active": 1, "item_ids": 1})
        if not cupboard or (source.get("require_active", False) and not cupboard.get("active")):
            return set()
        return set(await _resolved_historical_ids(cupboard.get("item_ids"), read_model))
    if source.get("kind") == "menu_catalog":
        # Menu-catalog evidence is derived from the catalog cocktails' canonical
        # recipe requirements by rebuild_location_drinks, not stored inventory.
        return set()
    # The commercial default remains location_inventory; no Tie's-House branch.
    if source and source.get("kind") not in {"location_inventory", None}:
        return set()
    rows = await db.location_inventory.find({"location_id": location_id, "in_stock": True}, {"_id": 0, "ingredient_id": 1}).to_list(length=None)
    return set(await _resolved_historical_ids((row.get("ingredient_id") for row in rows), read_model))


async def _active_menu_catalog_cocktail_ids(location_id: str) -> set[str]:
    """Return only active, canonical cocktail mappings from a location's menu."""
    rows = await db.location_catalog_items.find(
        {"location_id": location_id, "status": "active", "item_class": "cocktail"},
        {"_id": 0, "cocktail_id": 1},
    ).to_list(length=None)
    return {
        cocktail_id
        for row in rows
        if isinstance((cocktail_id := row.get("cocktail_id")), str) and cocktail_id.strip()
    }


async def rebuild_location_drinks(location_id: str, read_model: Optional[CanonicalReadModel] = None) -> int:
    """Replaceable derived capability cache; inventory remains authoritative elsewhere."""
    if read_model is None:
        read_model, _ = await _canonical_read_model_cache.get(RequestTiming())
    settings = await db.location_settings.find_one(
        {"location_id": location_id},
        {"_id": 0, "inventory_source": 1, "secondary_liquor_availability": 1, "mixer_availability": 1},
    )
    source = (settings or {}).get("inventory_source") or {}
    if source.get("kind") == "menu_catalog":
        catalog_cocktail_ids = await _active_menu_catalog_cocktail_ids(location_id)
        # The menu is the complete capability universe.  Recipe requirements for
        # those canonical cocktails are the menu's effective ingredient evidence.
        cocktails = [
            cocktail for cocktail in read_model.cocktails
            if str(cocktail.get("cocktail_id") or "") in catalog_cocktail_ids
        ]
        inventory_ids = {
            ingredient_id
            for cocktail in cocktails
            for ingredient_id in read_model.required_ingredients_by_cocktail.get(
                str(cocktail.get("cocktail_id") or ""), ()
            )
            if ingredient_id is not None
        }
    else:
        cocktails = read_model.cocktails
        inventory_ids = await resolve_effective_location_inventory(location_id, read_model, settings)
    now = _now()
    records = []
    for cocktail in cocktails:
        cocktail_id = str(cocktail.get("cocktail_id") or "")
        required = list(read_model.required_ingredients_by_cocktail.get(cocktail_id, ()))
        if not cocktail_id or not required:
            can_make, matched, missing, has_unresolved_requirement = False, [], [], True
        else:
            can_make, matched, missing, has_unresolved_requirement = _location_inventory_satisfies_requirements(
                required, inventory_ids, read_model.ingredients_by_id, settings
            )
        records.append({
            "location_id": location_id,
            "cocktail_id": cocktail_id,
            "can_make": can_make,
            "matched_ingredient_ids": matched,
            "missing_ingredient_ids": missing,
            "has_unresolved_requirement": has_unresolved_requirement,
            "computed_at": now,
            "source_version": "effective_inventory_v2",
        })
    await db.location_drinks.delete_many({"location_id": location_id})
    if records:
        await db.location_drinks.insert_many(records)
    return len(records)


async def refresh_cupboard_backed_locations(changed_user_id: str) -> None:
    """Synchronous v1 refresh after an application-mediated host-cupboard write."""
    settings_rows = await db.location_settings.find({"inventory_source.kind": "user_cupboard", "inventory_source.user_id": changed_user_id}, {"_id": 0, "location_id": 1}).to_list(length=None)
    if not settings_rows:
        return
    read_model, _ = await _canonical_read_model_cache.get(RequestTiming())
    for settings in settings_rows:
        location_id = settings.get("location_id")
        if location_id:
            await rebuild_location_drinks(str(location_id), read_model)


@api_router.get("/drinks/match", response_model=MatchResponse)
async def match_drink(
    strong: int, fancy: int, comfort: int, party: int, thirsty: int,
    scoring: str = "differential",
    limit: int = 5,
    alcohols: Optional[str] = None,
    glasses: Optional[str] = None,
    location_id: Optional[str] = None,
    authorization: Optional[str] = Header(None),
    response: Response = None,
):
    """Return top canonical cocktails after What-I-Want, location/cupboard and user filters.

    `alcohols` and `glasses` retain the existing mobile API names for backward
    compatibility. Main-ingredient matching is canonical: the cocktail's
    `main_ingredient_ids` resolve to canonical ingredients and their
    `primary_ingredient` classification. Cupboard matching uses structured
    `cocktail_ingredients` and canonical ingredient identity/substitution.
    """
    if scoring not in ("differential", "alternate"):
        raise HTTPException(status_code=400, detail="scoring must be 'differential' or 'alternate'")
    if limit < 1 or limit > 50:
        raise HTTPException(status_code=400, detail="limit must be 1..50")

    timing = RequestTiming()
    me: Optional[User] = None
    if authorization:
        with timing.phase("authentication"):
            try:
                me = await current_user(authorization)
            except HTTPException:
                me = None

    slider_vals = {"strong": strong, "fancy": fancy, "comfort": comfort,
                   "party": party, "thirsty": thirsty}
    for name, v in slider_vals.items():
        if not isinstance(v, int) or v < 1 or v > 10:
            raise HTTPException(status_code=400, detail=f"{name} must be an integer between 1 and 10")

    alcohol_filters = {a.strip().lower() for a in (alcohols or "").split(",") if a.strip()}
    unknown = alcohol_filters - ALLOWED_ALCOHOL_FILTERS
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown alcohol filters: {sorted(unknown)}")

    glass_filters = {g.strip().lower() for g in (glasses or "").split(",") if g.strip()}
    unknown_g = glass_filters - ALLOWED_GLASS_FILTERS
    if unknown_g:
        raise HTTPException(status_code=400, detail=f"unknown glass filters: {sorted(unknown_g)}")

    dim_vals = {SLIDER_TO_DIM[k]: v for k, v in slider_vals.items()}
    user_sum = sum(dim_vals.values())
    user_ratios = {d: dim_vals[d] / user_sum for d in DIMS}

    read_model, cache_state = await _canonical_read_model_cache.get(timing)
    canonical_docs = list(read_model.cocktails)
    if not canonical_docs:
        raise HTTPException(status_code=404, detail="No drinks in database")

    ingredients_by_id = read_model.ingredients_by_id
    with timing.phase("ingredient_resolution"):
        main_ingredient_map = await _resolved_id_map(
            (
                ingredient_id
                for cocktail in canonical_docs
                for ingredient_id in cocktail.get("main_ingredient_ids") or []
            ),
            read_model,
        )

    # What I Want: canonical main ingredient filter.
    with timing.phase("filtering"):
        if alcohol_filters:
            spirit_filters = alcohol_filters - {"non_alcoholic"}
            want_nonalc = "non_alcoholic" in alcohol_filters
            kept = []
            for c in canonical_docs:
                ok = want_nonalc and str(c.get("alcohol_class") or "").strip().lower().startswith("non")
                if not ok and spirit_filters:
                    for source_id in _int_ids(c.get("main_ingredient_ids")):
                        ing = ingredients_by_id.get(main_ingredient_map.get(source_id)) or {}
                        if str(ing.get("primary_ingredient") or "").strip().lower() in spirit_filters:
                            ok = True
                            break
                if ok:
                    kept.append(c)
            canonical_docs = kept

        # Drink Style filter-family membership is glass-owned. Card icon
        # identity remains the cocktail's glass_category_id.
        glasses_by_id = read_model.glasses_by_id

        # What I Want: OR within Drink Style, based solely on canonical
        # glasses.filter_families. No legacy-name fallback or taxonomy is allowed.
        if glass_filters:
            canonical_docs = [
                c for c in canonical_docs
                if glass_filters.intersection(set((glasses_by_id.get(str(c.get("glass_id"))) or {}).get("filter_families") or []))
            ]

    # Checked-in location replaces home cupboard constraint.
    if location_id:
        with timing.phase("location_constraints"):
            can_make_ids = {
                row["cocktail_id"]
                async for row in db.location_drinks.find(
                    {"location_id": location_id, "can_make": True}, {"_id": 0, "cocktail_id": 1}
                )
            }
            canonical_docs = [c for c in canonical_docs if c.get("cocktail_id") in can_make_ids]
    elif me:
        with timing.phase("cupboard_lookup"):
            cupboard = await db.user_cupboard.find_one({"user_id": me.user_id}, {"_id": 0})
        if cupboard and cupboard.get("active"):
            with timing.phase("ingredient_normalization"):
                saved_ids = set(await _resolved_historical_ids(cupboard.get("item_ids"), read_model))
            cocktail_ids = [c["cocktail_id"] for c in canonical_docs]
            with timing.phase("recipe_requirement_acquisition"):
                requirements = await _canonical_recipe_requirements(cocktail_ids, read_model)

            def cupboard_satisfies(cid: str) -> bool:
                reqs = requirements.get(cid)
                if not reqs:  # No structured recipe => cannot claim "can make".
                    return False
                return _inventory_satisfies_requirements(reqs, saved_ids, ingredients_by_id)[0]

            with timing.phase("cupboard_matching"):
                canonical_docs = [c for c in canonical_docs if cupboard_satisfies(c["cocktail_id"])]

    with timing.phase("response_shaping"):
        docs = [_canonical_to_drink(d, glasses_by_id.get(str(d.get("glass_id")))) for d in canonical_docs]

    blocked_ids: set[str] = set()
    favorite_ids: set[str] = set()
    if me:
        with timing.phase("blocked_favorites"):
            blocked_ids = {b["drink_id"] async for b in db.blocked.find({"user_id": me.user_id}, {"_id": 0, "drink_id": 1})}
            favorite_ids = {f["drink_id"] async for f in db.favorites.find({"user_id": me.user_id}, {"_id": 0, "drink_id": 1})}
    if blocked_ids:
        docs = [d for d in docs if d["id"] not in blocked_ids]

    def score_of(doc: dict) -> float:
        total = doc["fancy"] + doc["dark"] + doc["thirsty"] + doc["calm"] + doc["celebrate"]
        if total <= 0:
            return float("inf")
        diff = sum(abs(user_ratios[d] - doc[d] / total) for d in DIMS)
        if scoring == "alternate":
            diff += abs(total - user_sum) / user_sum
        return diff

    with timing.phase("scoring"):
        scored_all = [(d, score_of(d)) for d in docs]
    with timing.phase("sorting_selection"):
        scored_all.sort(key=lambda x: x[1])
        top = scored_all[:limit]
    pin_slot = 4
    if favorite_ids and len(top) > pin_slot:
        top5_ids = {d["id"] for d, _ in top[:5]}
        if not (favorite_ids & top5_ids):
            best_fav_pair = next(((d, score) for d, score in scored_all if d["id"] in favorite_ids), None)
            if best_fav_pair:
                top = (top[:pin_slot] + [best_fav_pair] + top[pin_slot:])[:limit]

    with timing.phase("serialization"):
        results = [MatchItem(drink=Drink(**d), score=float(score), is_favorite=d["id"] in favorite_ids) for d, score in top]
        result = MatchResponse(results=results, scoring=scoring, query=slider_vals, query_mapped=dim_vals)
    timing.set_response_header(response, cache_state)
    return result


@api_router.get("/drinks/shake-shot", response_model=ShakeShotResponse)
async def shake_a_shot(
    alcohols: Optional[str] = None,
    use_cupboard: bool = False,
    location_id: Optional[str] = None,
    previous_drink_id: Optional[str] = None,
    authorization: Optional[str] = Header(None),
    response: Response = None,
):
    """Select a random canonical Shot without using preference-slider scoring.

    Premium constraints are supplied only by an entitled client. The endpoint
    deliberately does not infer entitlement or retain it: it applies the
    requested main-ingredient/cupboard constraints, the authenticated user's
    blocked list, and canonical location availability.
    """
    timing = RequestTiming()
    me: Optional[User] = None
    if authorization:
        try:
            me = await current_user(authorization)
        except HTTPException:
            me = None

    alcohol_filters = {a.strip().lower() for a in (alcohols or "").split(",") if a.strip()}
    unknown = alcohol_filters - ALLOWED_ALCOHOL_FILTERS
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown alcohol filters: {sorted(unknown)}")

    read_model, cache_state = await _canonical_read_model_cache.get(timing)
    canonical_docs = [dict(cocktail) for cocktail in read_model.cocktails]
    if not canonical_docs:
        timing.set_response_header(response, cache_state)
        return ShakeShotResponse()

    # Shot membership comes only from the locked canonical glasses record,
    # never from legacy glass names or cocktail recipe/category text.
    glasses_by_id = read_model.glasses_by_id
    ingredients_by_id = read_model.ingredients_by_id
    with timing.phase("filtering"):
        canonical_docs = [
            cocktail for cocktail in canonical_docs
            if "shot" in set((glasses_by_id.get(str(cocktail.get("glass_id"))) or {}).get("filter_families") or [])
        ]

    with timing.phase("ingredient_resolution"):
        main_ingredient_map = await _resolved_id_map(
            (
                ingredient_id
                for cocktail in canonical_docs
                for ingredient_id in cocktail.get("main_ingredient_ids") or []
            ),
            read_model,
        )

    if alcohol_filters:
        spirit_filters = alcohol_filters - {"non_alcoholic"}
        wants_non_alcoholic = "non_alcoholic" in alcohol_filters
        with timing.phase("filtering"):
            filtered_docs = []
            for cocktail in canonical_docs:
                matches = wants_non_alcoholic and str(cocktail.get("alcohol_class") or "").strip().lower().startswith("non")
                if not matches and spirit_filters:
                    matches = any(
                        str((ingredients_by_id.get(main_ingredient_map.get(ingredient_id)) or {}).get("primary_ingredient") or "").strip().lower() in spirit_filters
                        for ingredient_id in _int_ids(cocktail.get("main_ingredient_ids"))
                    )
                if matches:
                    filtered_docs.append(cocktail)
            canonical_docs = filtered_docs

    # Checked-in location is authoritative and replaces the home-cupboard
    # constraint, exactly as normal match results do.
    if location_id:
        with timing.phase("constraints"):
            can_make_ids = {
                row["cocktail_id"]
                async for row in db.location_drinks.find(
                    {"location_id": location_id, "can_make": True}, {"_id": 0, "cocktail_id": 1}
                )
            }
            canonical_docs = [cocktail for cocktail in canonical_docs if cocktail.get("cocktail_id") in can_make_ids]
    elif me and use_cupboard:
        with timing.phase("constraints"):
            cupboard = await db.user_cupboard.find_one({"user_id": me.user_id}, {"_id": 0})
        if cupboard and cupboard.get("active"):
            with timing.phase("constraints"):
                saved_ids = set(await _resolved_historical_ids(cupboard.get("item_ids"), read_model))
                cocktail_ids = [cocktail["cocktail_id"] for cocktail in canonical_docs]
                requirements = await _canonical_recipe_requirements(cocktail_ids, read_model)

                def cupboard_satisfies(cocktail_id: str) -> bool:
                    required_ids = requirements.get(cocktail_id)
                    if not required_ids:
                        return False
                    return _inventory_satisfies_requirements(required_ids, saved_ids, ingredients_by_id)[0]

                canonical_docs = [
                    cocktail for cocktail in canonical_docs if cupboard_satisfies(cocktail["cocktail_id"])
                ]

    if me:
        with timing.phase("constraints"):
            blocked_ids = {
                row["drink_id"] async for row in db.blocked.find({"user_id": me.user_id}, {"_id": 0, "drink_id": 1})
            }
            canonical_docs = [cocktail for cocktail in canonical_docs if cocktail["cocktail_id"] not in blocked_ids]

    # Never immediately repeat if another eligible Shot exists.
    if previous_drink_id and len(canonical_docs) > 1:
        with timing.phase("filtering"):
            canonical_docs = [cocktail for cocktail in canonical_docs if cocktail["cocktail_id"] != previous_drink_id]

    if not canonical_docs:
        timing.set_response_header(response, cache_state)
        return ShakeShotResponse()
    with timing.phase("sorting_selection"):
        selected = secrets.choice(canonical_docs)
    with timing.phase("serialization"):
        result = ShakeShotResponse(drink=Drink(**_canonical_to_drink(selected, glasses_by_id.get(str(selected.get("glass_id"))))))
    timing.set_response_header(response, cache_state)
    return result


class DrinkSearchResult(BaseModel):
    id: str
    name: str
    glass: str = ""
    glass_category_id: str = ""
    ingredients: str = ""
    is_favorite: bool = False


@api_router.get("/drinks/search", response_model=List[DrinkSearchResult])
async def search_drinks(q: str, limit: int = 20, authorization: Optional[str] = Header(None)):
    """Search the canonical cocktail catalog by name (case-insensitive substring)."""
    q = (q or "").strip()
    if not q:
        return []
    if limit < 1 or limit > 50:
        limit = 20
    cursor = db.cocktails.find(
        {"status": "active", "name": {"$regex": re.escape(q), "$options": "i"}},
        {"_id": 0},
    ).limit(limit)
    docs = await cursor.to_list(length=limit)
    favorite_ids: set[str] = set()
    if authorization:
        try:
            user = await current_user(authorization)
            favorite_ids = {row["drink_id"] async for row in db.favorites.find({"user_id": user.user_id}, {"_id": 0, "drink_id": 1})}
        except HTTPException:
            pass
    glasses_by_id = await _canonical_glasses_by_id()
    return [
        DrinkSearchResult(
            id=d["cocktail_id"],
            name=d.get("name") or "",
            glass=(glasses_by_id.get(str(d.get("glass_id"))) or {}).get("display_name") or d.get("glass_id") or "",
            glass_category_id=d.get("glass_category_id") or "",
            ingredients=d.get("human_ingredients") or "",
            is_favorite=d["cocktail_id"] in favorite_ids,
        )
        for d in docs
    ]


# NOTE: this catch-all-by-id route must stay AFTER /drinks/search —
# FastAPI matches routes in registration order, and {drink_id} would
# otherwise swallow "/drinks/search" as if "search" were the id.
@api_router.get("/drinks/{drink_id}", response_model=Drink)
async def get_drink(drink_id: str):
    doc = await db.cocktails.find_one(
        {"cocktail_id": drink_id, "status": "active"}, {"_id": 0}
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Drink not found")
    glasses_by_id = await _canonical_glasses_by_id()
    return Drink(**_canonical_to_drink(doc, glasses_by_id.get(str(doc.get("glass_id")))))


# ================================================================
#                     AUTH — Emergent Google OAuth
# ================================================================

EMERGENT_SESSION_DATA_URL = "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data"
SESSION_TTL_DAYS = 7
PREMIUM_PRODUCT_ID = "app.drinkthink.premium"
PREMIUM_PLATFORMS = {"ios", "android"}


def _premium_config(name: str) -> str:
    """Read a server-only Premium setting without ever returning its value."""
    value = os.environ.get(name)
    if not value:
        raise HTTPException(status_code=503, detail="Premium verification is not configured")
    return value


def _purchase_identity_hash(platform: str, identity: str) -> str:
    key = _premium_config("PREMIUM_PURCHASE_IDENTITY_HMAC_KEY").encode()
    return hmac.new(key, f"{platform}:{identity}".encode(), hashlib.sha256).hexdigest()


def _encrypt_verification_reference(reference: str) -> str:
    try:
        return Fernet(_premium_config("PREMIUM_VERIFICATION_REFERENCE_ENCRYPTION_KEY").encode()).encrypt(reference.encode()).decode()
    except (ValueError, TypeError):
        raise HTTPException(status_code=503, detail="Premium verification is not configured")


def _decode_jws_payload(value: str) -> dict:
    """Decode only to obtain a transaction id; Apple is queried for authority."""
    try:
        payload = value.split(".")[1] + "=" * (-len(value.split(".")[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode()))
    except (IndexError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid Apple transaction evidence")


async def _verify_apple_purchase(transaction_jws: str) -> tuple[str, str, str, bool, Optional[str]]:
    supplied = _decode_jws_payload(transaction_jws)
    transaction_id = str(supplied.get("transactionId") or "")
    if not transaction_id:
        raise HTTPException(status_code=400, detail="Invalid Apple transaction evidence")
    private_key = _premium_config("APPLE_APP_STORE_PRIVATE_KEY").replace("\\n", "\n")
    now = int(_now().timestamp())
    token = jwt.encode({"iss": _premium_config("APPLE_APP_STORE_ISSUER_ID"), "iat": now, "exp": now + 300, "aud": "appstoreconnect-v1", "bid": _premium_config("APPLE_BUNDLE_ID")}, private_key, algorithm="ES256", headers={"kid": _premium_config("APPLE_APP_STORE_KEY_ID")})
    environment = os.environ.get("APPLE_APP_STORE_ENVIRONMENT", "Production").lower()
    base = "https://api.storekit-sandbox.apple.com" if environment == "sandbox" else "https://api.storekit.itunes.apple.com"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client_http:
            response = await client_http.get(f"{base}/inApps/v1/transactions/{transaction_id}", headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Apple verification is temporarily unavailable")
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Apple transaction could not be verified")
    server_payload = _decode_jws_payload(str(response.json().get("signedTransactionInfo") or ""))
    if server_payload.get("bundleId") != _premium_config("APPLE_BUNDLE_ID") or server_payload.get("productId") != PREMIUM_PRODUCT_ID:
        raise HTTPException(status_code=400, detail="Apple transaction does not match DrinkThink Premium")
    original = str(server_payload.get("originalTransactionId") or "")
    if not original:
        raise HTTPException(status_code=400, detail="Apple transaction is incomplete")
    revoked = bool(server_payload.get("revocationDate"))
    return original, transaction_id, "revoked" if revoked else "active", revoked, server_payload.get("revocationReason")


async def _google_access_token() -> str:
    try:
        credentials = json.loads(_premium_config("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON"))
        now = int(_now().timestamp())
        assertion = jwt.encode({"iss": credentials["client_email"], "scope": "https://www.googleapis.com/auth/androidpublisher", "aud": "https://oauth2.googleapis.com/token", "iat": now, "exp": now + 3600}, credentials["private_key"], algorithm="RS256")
        async with httpx.AsyncClient(timeout=15.0) as client_http:
            response = await client_http.post("https://oauth2.googleapis.com/token", data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
        response.raise_for_status()
        return str(response.json()["access_token"])
    except (KeyError, ValueError, jwt.PyJWTError, httpx.HTTPError):
        raise HTTPException(status_code=503, detail="Google Play verification is unavailable")


async def _verify_google_purchase(purchase_token: str) -> tuple[str, str, Optional[str]]:
    access_token = await _google_access_token()
    package = _premium_config("GOOGLE_PLAY_PACKAGE_NAME")
    url = f"https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{package}/purchases/productsv2/tokens/{purchase_token}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client_http:
            response = await client_http.get(url, headers={"Authorization": f"Bearer {access_token}"})
            if response.status_code != 200:
                raise HTTPException(status_code=400, detail="Google Play purchase could not be verified")
            data = response.json()
            state = (data.get("purchaseStateContext") or {}).get("purchaseState")
            products = [item.get("productId") for item in data.get("productLineItem", [])]
            if state != "PURCHASED" or PREMIUM_PRODUCT_ID not in products:
                raise HTTPException(status_code=400, detail="Google Play purchase is not valid for DrinkThink Premium")
            if data.get("acknowledgementState") != "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED":
                ack = await client_http.post(url + ":acknowledge", headers={"Authorization": f"Bearer {access_token}"}, json={})
                ack.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Google Play verification is temporarily unavailable")
    return purchase_token, purchase_token, None


class User(BaseModel):
    user_id: str
    email: str
    name: str = ""
    picture: str = ""
    created_at: str


class SessionRequest(BaseModel):
    session_id: str


class SessionResponse(BaseModel):
    session_token: str
    user: User


class PremiumEntitlementResponse(BaseModel):
    platform: Literal["ios", "android"]
    product_id: str = PREMIUM_PRODUCT_ID
    premium: bool
    verified_at: Optional[str] = None


class PremiumVerificationRequest(BaseModel):
    platform: Literal["ios", "android"]
    product_id: str
    transaction_jws: Optional[str] = None
    purchase_token: Optional[str] = None


async def _entitlement_response(user_id: str, platform: str) -> PremiumEntitlementResponse:
    doc = await db.premium_entitlements.find_one({"user_id": user_id, "platform": platform, "product_id": PREMIUM_PRODUCT_ID}, {"_id": 0})
    if not doc or doc.get("status") != "active":
        return PremiumEntitlementResponse(platform=platform, premium=False)
    verified_at = doc.get("last_verified_at")
    return PremiumEntitlementResponse(platform=platform, premium=True, verified_at=verified_at.isoformat() if isinstance(verified_at, datetime) else verified_at)


async def current_user(authorization: Optional[str] = Header(None)) -> User:
    """Resolve the bearer token into a User. 401s on any failure."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    session = await db.user_sessions.find_one({"session_token": token}, {"_id": 0})
    if not session:
        raise HTTPException(status_code=401, detail="Invalid session")
    exp = session.get("expires_at")
    if isinstance(exp, datetime):
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < _now():
            raise HTTPException(status_code=401, detail="Session expired")
    user = await db.users.find_one({"user_id": session["user_id"]}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return User(**user)


@api_router.get("/premium/entitlement", response_model=PremiumEntitlementResponse)
async def premium_entitlement(platform: Literal["ios", "android"], user: User = Depends(current_user)):
    return await _entitlement_response(user.user_id, platform)


@api_router.post("/premium/verify", response_model=PremiumEntitlementResponse)
async def verify_premium_purchase(body: PremiumVerificationRequest, user: User = Depends(current_user)):
    if body.product_id != PREMIUM_PRODUCT_ID:
        raise HTTPException(status_code=400, detail="Unsupported Premium product")
    if body.platform == "ios":
        if not body.transaction_jws:
            raise HTTPException(status_code=400, detail="Apple transaction evidence is required")
        identity, reference, status, _revoked, reason = await _verify_apple_purchase(body.transaction_jws)
    else:
        if not body.purchase_token:
            raise HTTPException(status_code=400, detail="Google Play purchase token is required")
        identity, reference, reason = await _verify_google_purchase(body.purchase_token)
        status = "active"
    identity_hash = _purchase_identity_hash(body.platform, identity)
    existing = await db.premium_entitlements.find_one({"platform": body.platform, "purchase_identity_hash": identity_hash}, {"_id": 0})
    if existing and existing.get("user_id") != user.user_id:
        raise HTTPException(status_code=409, detail="This Premium purchase is already associated with another DrinkThink account")
    now = _now()
    if status == "revoked":
        await db.premium_entitlements.update_one({"platform": body.platform, "purchase_identity_hash": identity_hash}, {"$set": {"status": "revoked", "last_verified_at": now, "revoked_at": now, "revocation_reason": str(reason or "store_revocation")}}, upsert=bool(existing))
        return PremiumEntitlementResponse(platform=body.platform, premium=False)
    try:
        await db.premium_entitlements.update_one(
            {"platform": body.platform, "purchase_identity_hash": identity_hash},
            {"$setOnInsert": {"user_id": user.user_id, "platform": body.platform, "product_id": PREMIUM_PRODUCT_ID, "purchase_identity_hash": identity_hash, "first_verified_at": now},
             "$set": {"verification_reference_encrypted": _encrypt_verification_reference(reference), "status": "active", "last_verified_at": now, "revoked_at": None, "revocation_reason": None}},
            upsert=True,
        )
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="This Premium purchase is already associated with another DrinkThink account")
    return await _entitlement_response(user.user_id, body.platform)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@api_router.post("/auth/session", response_model=SessionResponse)
async def auth_session(body: SessionRequest):
    """Exchange a one-time session_id from Emergent's OAuth callback for a
    7-day session_token stored server-side.
    """
    session_id = body.session_id
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")

    async with httpx.AsyncClient(timeout=15.0) as client_http:
        try:
            r = await client_http.get(
                EMERGENT_SESSION_DATA_URL,
                headers={"X-Session-ID": session_id},
            )
        except httpx.HTTPError as e:
            logger.exception("Emergent auth network error")
            raise HTTPException(status_code=502, detail=str(e))
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid or used session_id")

    data = r.json()
    email = (data.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="No email in Emergent response")
    name = data.get("name") or ""
    picture = data.get("picture") or ""
    session_token = data.get("session_token")
    if not session_token:
        raise HTTPException(status_code=502, detail="No session_token from Emergent")

    # Upsert user by email
    existing = await db.users.find_one({"email": email}, {"_id": 0})
    if existing:
        user_id = existing["user_id"]
        await db.users.update_one(
            {"user_id": user_id},
            {"$set": {"name": name, "picture": picture}},
        )
        user_doc = {**existing, "name": name, "picture": picture}
    else:
        user_id = f"user_{uuid.uuid4().hex[:12]}"
        user_doc = {
            "user_id": user_id,
            "email": email,
            "name": name,
            "picture": picture,
            "created_at": _now().isoformat(),
        }
        await db.users.insert_one(user_doc.copy())

    # Store session
    await db.user_sessions.insert_one({
        "session_token": session_token,
        "user_id": user_id,
        "created_at": _now(),
        "expires_at": _now() + timedelta(days=SESSION_TTL_DAYS),
    })

    return SessionResponse(session_token=session_token, user=User(**user_doc))


@api_router.get("/auth/me", response_model=User)
async def auth_me(user: User = Depends(current_user)):
    return user


@api_router.post("/auth/logout")
async def auth_logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        await db.user_sessions.delete_one({"session_token": token})
    return {"ok": True}


class AccountDeletionError(RuntimeError):
    """Raised when account deletion cannot complete atomically."""


async def _delete_account_data(user_id: str, session) -> None:
    """Delete every currently implemented consumer record owned by one user.

    This helper must run inside the transaction opened by ``delete_account``.
    Pending drink shares are deleted whether the user was sender or recipient:
    there is no independent recipient-owned record to preserve, and deletion
    prevents the removed user's email, name, or ID from remaining in another
    user's pending-share context.
    """
    await db.user_sessions.delete_many({"user_id": user_id}, session=session)
    await db.favorites.delete_many({"user_id": user_id}, session=session)
    await db.blocked.delete_many({"user_id": user_id}, session=session)
    await db.user_cupboard.delete_many({"user_id": user_id}, session=session)
    await db.pending_shares.delete_many(
        {"$or": [{"sender_user_id": user_id}, {"recipient_user_id": user_id}]},
        session=session,
    )
    await db.share_checkins.delete_many({"sharer_user_id": user_id}, session=session)
    await db.premium_entitlements.delete_many({"user_id": user_id}, session=session)
    result = await db.users.delete_one({"user_id": user_id}, session=session)
    if result.deleted_count != 1:
        raise AccountDeletionError("Authenticated user record was not deleted")


@api_router.delete("/account")
async def delete_account(user: User = Depends(current_user)):
    """Permanently remove the authenticated user's currently stored data.

    A MongoDB transaction is mandatory: if the deployment cannot provide
    transactions or any write fails, the endpoint returns an operational error
    and does not report deletion as successful.
    """
    try:
        async with await client.start_session() as session:
            async with session.start_transaction():
                await _delete_account_data(user.user_id, session)
    except (AccountDeletionError, PyMongoError):
        # Do not include user identifiers, emails, sessions, or request data in
        # operational logs.
        logger.exception("Account deletion transaction failed")
        raise HTTPException(
            status_code=503,
            detail="Account deletion could not be completed. Please try again.",
        )
    return {"ok": True}


# ================================================================
#                  ME — Favorites & Blocked drinks
# ================================================================

class DrinkStatus(BaseModel):
    drink_id: str
    is_favorite: bool
    is_blocked: bool


async def _drink_exists_or_404(drink_id: str) -> None:
    if not await db.cocktails.find_one(
        {"cocktail_id": drink_id, "status": "active"}, {"_id": 0, "cocktail_id": 1}
    ):
        raise HTTPException(status_code=404, detail="Drink not found")


@api_router.get("/me/status/{drink_id}", response_model=DrinkStatus)
async def me_status(drink_id: str, user: User = Depends(current_user)):
    await _drink_exists_or_404(drink_id)
    fav = await db.favorites.find_one(
        {"user_id": user.user_id, "drink_id": drink_id}, {"_id": 0}
    )
    blk = await db.blocked.find_one(
        {"user_id": user.user_id, "drink_id": drink_id}, {"_id": 0}
    )
    return DrinkStatus(drink_id=drink_id, is_favorite=bool(fav), is_blocked=bool(blk))


@api_router.post("/me/favorites/{drink_id}")
async def add_favorite(drink_id: str, user: User = Depends(current_user)):
    await _drink_exists_or_404(drink_id)
    # Adding a favorite un-blocks it (mutually exclusive states).
    await db.blocked.delete_one({"user_id": user.user_id, "drink_id": drink_id})
    # New favorites go to the end of the user's custom order.
    existing_count = await db.favorites.count_documents({"user_id": user.user_id})
    await db.favorites.update_one(
        {"user_id": user.user_id, "drink_id": drink_id},
        {"$setOnInsert": {"created_at": _now(), "order": existing_count}},
        upsert=True,
    )
    return {"ok": True}


@api_router.delete("/me/favorites/{drink_id}")
async def remove_favorite(drink_id: str, user: User = Depends(current_user)):
    await db.favorites.delete_one({"user_id": user.user_id, "drink_id": drink_id})
    return {"ok": True}


class ReorderFavoritesRequest(BaseModel):
    drink_ids: List[str]


@api_router.post("/me/favorites/reorder")
async def reorder_favorites(body: ReorderFavoritesRequest, user: User = Depends(current_user)):
    """Persist a user's custom drag-and-drop order for their favorites list.
    drink_ids must be the full, ordered list of the user's favorite ids."""
    for position, drink_id in enumerate(body.drink_ids):
        await db.favorites.update_one(
            {"user_id": user.user_id, "drink_id": drink_id},
            {"$set": {"order": position}},
        )
    return {"ok": True}


@api_router.get("/me/favorites", response_model=List[Drink])
async def list_favorites(user: User = Depends(current_user)):
    rows = await db.favorites.find(
        {"user_id": user.user_id}, {"_id": 0}
    ).sort([("order", 1), ("created_at", 1)]).to_list(length=None)
    if not rows:
        return []
    ids = [r["drink_id"] for r in rows]
    glasses_by_id = await _canonical_glasses_by_id()
    drinks_by_id = {
        d["cocktail_id"]: _canonical_to_drink(d, glasses_by_id.get(str(d.get("glass_id"))))
        async for d in db.cocktails.find(
            {"cocktail_id": {"$in": ids}, "status": "active"}, {"_id": 0}
        )
    }
    return [Drink(**drinks_by_id[i]) for i in ids if i in drinks_by_id]


@api_router.post("/me/blocked/{drink_id}")
async def add_block(drink_id: str, user: User = Depends(current_user)):
    await _drink_exists_or_404(drink_id)
    # Blocking a drink un-favorites it.
    await db.favorites.delete_one({"user_id": user.user_id, "drink_id": drink_id})
    await db.blocked.update_one(
        {"user_id": user.user_id, "drink_id": drink_id},
        {"$setOnInsert": {"created_at": _now()}},
        upsert=True,
    )
    return {"ok": True}


@api_router.delete("/me/blocked/{drink_id}")
async def remove_block(drink_id: str, user: User = Depends(current_user)):
    await db.blocked.delete_one({"user_id": user.user_id, "drink_id": drink_id})
    return {"ok": True}


@api_router.get("/me/blocked", response_model=List[Drink])
async def list_blocked(user: User = Depends(current_user)):
    rows = await db.blocked.find(
        {"user_id": user.user_id}, {"_id": 0}
    ).sort("created_at", -1).to_list(length=None)
    if not rows:
        return []
    ids = [r["drink_id"] for r in rows]
    glasses_by_id = await _canonical_glasses_by_id()
    drinks_by_id = {
        d["cocktail_id"]: _canonical_to_drink(d, glasses_by_id.get(str(d.get("glass_id"))))
        async for d in db.cocktails.find(
            {"cocktail_id": {"$in": ids}, "status": "active"}, {"_id": 0}
        )
    }
    return [Drink(**drinks_by_id[i]) for i in ids if i in drinks_by_id]


# ================================================================
#                     SHARE — in-app drink sharing
# ================================================================

class ShareDrinkRequest(BaseModel):
    drink_id: str
    recipient_email: str


class PendingShare(BaseModel):
    share_id: str
    drink_id: str
    drink_name: str
    sender_name: str
    sender_email: str
    created_at: str


@api_router.post("/share/drink")
async def share_drink(body: ShareDrinkRequest, user: User = Depends(current_user)):
    await _drink_exists_or_404(body.drink_id)
    recipient_email = (body.recipient_email or "").strip().lower()
    if not recipient_email:
        raise HTTPException(status_code=400, detail="recipient_email required")
    if recipient_email == user.email:
        raise HTTPException(status_code=400, detail="Can't share a drink with yourself")

    recipient = await db.users.find_one({"email": recipient_email}, {"_id": 0})
    if not recipient:
        # Per product decision: no invite-to-download flow here — the
        # frontend directs the sender to the native "Send" option instead.
        raise HTTPException(
            status_code=404,
            detail="That email isn't a registered DrinkThink user",
        )

    share_id = f"share_{uuid.uuid4().hex[:12]}"
    await db.pending_shares.insert_one({
        "share_id": share_id,
        "drink_id": body.drink_id,
        "sender_user_id": user.user_id,
        "sender_name": user.name or user.email,
        "sender_email": user.email,
        "recipient_user_id": recipient["user_id"],
        "recipient_email": recipient_email,
        "status": "pending",
        "created_at": _now(),
    })
    return {"ok": True, "share_id": share_id}


@api_router.get("/me/pending-shares", response_model=List[PendingShare])
async def list_pending_shares(user: User = Depends(current_user)):
    rows = await db.pending_shares.find(
        {"recipient_user_id": user.user_id, "status": "pending"}, {"_id": 0}
    ).sort("created_at", -1).to_list(length=None)
    if not rows:
        return []
    ids = [r["drink_id"] for r in rows]
    names_by_id = {
        d["cocktail_id"]: d["name"]
        async for d in db.cocktails.find(
            {"cocktail_id": {"$in": ids}, "status": "active"},
            {"_id": 0, "cocktail_id": 1, "name": 1},
        )
    }
    return [
        PendingShare(
            share_id=r["share_id"],
            drink_id=r["drink_id"],
            drink_name=names_by_id.get(r["drink_id"], "Unknown drink"),
            sender_name=r["sender_name"],
            sender_email=r["sender_email"],
            created_at=r["created_at"].isoformat() if isinstance(r["created_at"], datetime) else str(r["created_at"]),
        )
        for r in rows
    ]


@api_router.post("/me/pending-shares/{share_id}/accept")
async def accept_pending_share(share_id: str, user: User = Depends(current_user)):
    share = await db.pending_shares.find_one(
        {"share_id": share_id, "recipient_user_id": user.user_id, "status": "pending"},
        {"_id": 0},
    )
    if not share:
        raise HTTPException(status_code=404, detail="Pending share not found")
    await db.favorites.update_one(
        {"user_id": user.user_id, "drink_id": share["drink_id"]},
        {"$setOnInsert": {"created_at": _now()}},
        upsert=True,
    )
    await db.pending_shares.update_one(
        {"share_id": share_id}, {"$set": {"status": "accepted"}}
    )
    return {"ok": True}


@api_router.post("/me/pending-shares/{share_id}/decline")
async def decline_pending_share(share_id: str, user: User = Depends(current_user)):
    result = await db.pending_shares.update_one(
        {"share_id": share_id, "recipient_user_id": user.user_id, "status": "pending"},
        {"$set": {"status": "declined"}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Pending share not found")
    return {"ok": True}


# ================================================================
#                REMOTE FEATURE FLAGS (toggle without republishing)
# ================================================================
# Single document in app_config holds every flag. The app fetches this on
# startup; flipping a flag here takes effect immediately for all users,
# no new build or store review needed. Toggle it either by editing the
# document directly in MongoDB Atlas's UI, or via the protected POST
# endpoint below using ADMIN_TOGGLE_KEY.

DEFAULT_FLAGS = {
    # Consumer ordering and new Premium sales must fail closed until enabled.
    "consumer_ordering_enabled": False,
    "consumer_premium_sales_enabled": False,
}


@api_router.get("/config")
async def get_remote_config():
    doc = await db.app_config.find_one({"_id": "flags"}, {"_id": 0})
    flags = {**DEFAULT_FLAGS, **(doc or {})}
    return flags


class UpdateFlagsRequest(BaseModel):
    flags: dict
    admin_key: str


@api_router.post("/config")
async def update_remote_config(body: UpdateFlagsRequest):
    expected_key = os.environ.get("ADMIN_TOGGLE_KEY")
    if not expected_key or body.admin_key != expected_key:
        raise HTTPException(status_code=403, detail="Invalid admin key")
    await db.app_config.update_one(
        {"_id": "flags"},
        {"$set": body.flags},
        upsert=True,
    )
    doc = await db.app_config.find_one({"_id": "flags"}, {"_id": 0})
    return {**DEFAULT_FLAGS, **(doc or {})}


@api_router.get("/ingredients")
async def get_ingredients_tree():
    """Project the integer canonical master into the existing 3-level mobile payload."""
    categories = await db.ingredient_categories.find({"status": "active"}, {"_id": 0}).sort("display_order", 1).to_list(length=None)
    ingredients = await db.ingredients.find({"status": "active"}, {"_id": 0}).to_list(length=None)
    by_category: dict[str, list[dict]] = {}
    for ingredient in ingredients:
        by_category.setdefault(ingredient.get("category_id"), []).append(ingredient)

    result = []
    for category in categories:
        rows = by_category.get(category["category_id"], [])
        groups: dict[str, list[dict]] = {}
        display_names: dict[str, str] = {}
        for row in rows:
            pname = str(row.get("primary_ingredient") or row.get("name") or "Other").strip()
            pkey = re.sub(r"[^a-z0-9]+", "_", pname.lower()).strip("_") or "other"
            groups.setdefault(pkey, []).append(row)
            display_names[pkey] = pname
        primaries = []
        for pkey in sorted(groups, key=lambda k: display_names[k].lower()):
            items = sorted(groups[pkey], key=lambda r: str(r.get("name") or "").lower())
            primaries.append({
                "id": f"{category['category_id']}:{pkey}",
                "name": display_names[pkey],
                # IDs are serialized as strings so the current React Native
                # selection model remains unchanged; save/match normalize to int.
                "items": [{"id": str(r["ingredient_id"]), "name": r.get("name") or str(r["ingredient_id"])} for r in items],
            })
        result.append({"id": category["category_id"], "name": category.get("name") or category["category_id"], "primaries": primaries})
    return result


class CupboardRequest(BaseModel):
    item_ids: List[str]
    active: bool

    @field_validator("item_ids")
    @classmethod
    def canonical_integer_ids(cls, values: List[str]) -> List[str]:
        normalized = []
        for value in values:
            try:
                normalized.append(str(parse_ingredient_id(value)))
            except IngredientResolutionError:
                raise ValueError(f"cupboard item_id must be a canonical integer ingredient ID: {value}")
        return normalized


@api_router.get("/me/cupboard")
async def get_cupboard(user: User = Depends(current_user)):
    doc = await db.user_cupboard.find_one({"user_id": user.user_id}, {"_id": 0})
    raw_ids = (doc or {}).get("item_ids", [])
    canonical_ids = await _resolved_historical_ids(raw_ids)
    # Reads do not mutate historical records. They present an active canonical
    # view until the controlled migration rewrites the stored values.
    return {"item_ids": [str(i) for i in canonical_ids], "active": (doc or {}).get("active", False)}


@api_router.post("/me/cupboard")
async def save_cupboard(body: CupboardRequest, user: User = Depends(current_user)):
    try:
        canonical_ids = await resolve_ingredient_ids(db, body.item_ids, strict=True)
    except IngredientResolutionError as error:
        raise HTTPException(status_code=400, detail=str(error))
    await db.user_cupboard.update_one(
        {"user_id": user.user_id},
        {"$set": {"item_ids": canonical_ids, "active": body.active}},
        upsert=True,
    )
    # This is a no-op unless this account is configured as a location's
    # inventory source. It never uses a checking-in visitor as inventory.
    await refresh_cupboard_backed_locations(user.user_id)
    return {"ok": True, "item_ids": [str(i) for i in canonical_ids], "active": body.active}


LOCATION_PROVISIONING_API_KEY_ENV = "DRINKTHINK_LOCATION_PROVISIONING_API_KEY"
LOCATION_INVENTORY_REBUILD_API_KEY_ENV = "DRINKTHINK_LOCATION_INVENTORY_REBUILD_API_KEY"
CANONICAL_LOCATION_STATUSES = {"active", "inactive"}


def _provisioning_authorized(authorization: Optional[str] = Header(None)) -> None:
    """Require the dedicated machine credential for location onboarding.

    Consumer sessions and the read-only AI token deliberately cannot provision
    locations. Railway provides the secret at runtime only.
    """
    expected = os.environ.get(LOCATION_PROVISIONING_API_KEY_ENV)
    if not expected:
        raise HTTPException(status_code=503, detail="Location provisioning is not configured")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    supplied = authorization.split(" ", 1)[1].strip()
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Invalid provisioning credential")


def _location_inventory_rebuild_authorized(authorization: Optional[str] = Header(None)) -> None:
    """Require the dedicated machine credential for rebuilding derived location drinks."""
    expected = os.environ.get(LOCATION_INVENTORY_REBUILD_API_KEY_ENV)
    if not expected:
        raise HTTPException(status_code=503, detail="Location inventory rebuild is not configured")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    supplied = authorization.split(" ", 1)[1].strip()
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Invalid rebuild credential")


class CanonicalLocationAddress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line1: str = Field(min_length=1, max_length=200)
    line2: Optional[str] = Field(default=None, max_length=200)
    city: str = Field(min_length=1, max_length=100)
    state: str = Field(min_length=2, max_length=100)
    postal_code: str = Field(min_length=1, max_length=20)
    country: str = Field(min_length=2, max_length=2, pattern=r"^[A-Za-z]{2}$")

    @field_validator("line1", "city", "state", "postal_code", "country", mode="before")
    @classmethod
    def non_blank_text(cls, value: Any) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValueError("address fields must not be blank")
        return value.upper() if value in {"us", "US"} else value

    @field_validator("line2", mode="before")
    @classmethod
    def normalized_line2(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        value = str(value).strip()
        return value or None


class LocationProvisioningRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location_id: str = Field(min_length=5, max_length=160, pattern=r"^loc_[a-z0-9_]+$")
    organization_id: str = Field(min_length=5, max_length=160, pattern=r"^org_[a-z0-9_]+$")
    name: str = Field(min_length=1, max_length=160)
    address: CanonicalLocationAddress
    timezone: str = Field(min_length=1, max_length=100)
    status: str

    @field_validator("name", "timezone", "status", mode="before")
    @classmethod
    def normalized_required_text(cls, value: Any) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValueError("value must not be blank")
        return value

    @field_validator("status")
    @classmethod
    def canonical_status(cls, value: str) -> str:
        if value not in CANONICAL_LOCATION_STATUSES:
            raise ValueError(f"status must be one of: {', '.join(sorted(CANONICAL_LOCATION_STATUSES))}")
        return value

    @field_validator("timezone")
    @classmethod
    def canonical_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone must be a valid IANA timezone") from error
        return value


def _canonical_location_address(address: CanonicalLocationAddress) -> dict[str, Any]:
    """Produce the exact persisted address shape, including a null line2."""
    return address.model_dump()


def _valid_canonical_geo(geo: Any) -> bool:
    if not isinstance(geo, dict) or geo.get("type") != "Point":
        return False
    coordinates = geo.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) != 2:
        return False
    longitude, latitude = coordinates
    return (
        isinstance(longitude, (int, float)) and not isinstance(longitude, bool)
        and isinstance(latitude, (int, float)) and not isinstance(latitude, bool)
        and math.isfinite(longitude) and math.isfinite(latitude)
        and -180 <= longitude <= 180 and -90 <= latitude <= 90
    )


def _same_canonical_address(stored: Any, requested: dict[str, Any]) -> bool:
    if not isinstance(stored, dict):
        return False
    return {key: stored.get(key) for key in requested} == requested


async def _provision_canonical_location(body: LocationProvisioningRequest) -> dict[str, Any]:
    """Create or safely update one canonical location without consumer side effects."""
    organization = await db.organizations.find_one({"organization_id": body.organization_id}, {"_id": 0, "organization_id": 1})
    if not organization:
        raise HTTPException(status_code=404, detail="Organization does not exist")

    address = _canonical_location_address(body.address)
    existing = await db.locations.find_one({"location_id": body.location_id}, {"_id": 0})
    if existing and existing.get("organization_id") != body.organization_id:
        raise HTTPException(status_code=409, detail="location_id is already assigned to another organization")

    same_address = bool(existing and _same_canonical_address(existing.get("address"), address))
    reuse_geo = bool(existing and same_address and _valid_canonical_geo(existing.get("geo")))
    geocoded = False
    geo = existing.get("geo") if reuse_geo else None
    if not reuse_geo:
        api_key = os.environ.get("GEOCODIO_API_KEY")
        if not api_key:
            raise HTTPException(status_code=503, detail="Location geocoding is not configured")
        try:
            geo = await geocode_canonical_address(address, api_key)
        except GeocodingError as error:
            # No write has occurred yet, so a failed address change leaves the
            # prior canonical address and coordinate intact.
            raise HTTPException(status_code=502, detail=f"Location geocoding failed: {error}")
        geocoded = True

    now = _now()
    update = {
        "$setOnInsert": {
            "location_id": body.location_id,
            "created_at": now,
            "pos_connection_id": None,
        },
        "$set": {
            "organization_id": body.organization_id,
            "name": body.name,
            "address": address,
            "geo": geo,
            "timezone": body.timezone,
            "status": body.status,
            "updated_at": now,
        },
    }
    await db.locations.update_one({"location_id": body.location_id}, update, upsert=True)
    saved = await db.locations.find_one({"location_id": body.location_id}, {"_id": 0})
    if not saved or not _valid_canonical_geo(saved.get("geo")):
        raise HTTPException(status_code=500, detail="Location provisioning did not persist canonical GeoJSON")
    return {
        "location_id": saved["location_id"],
        "organization_id": saved["organization_id"],
        "name": saved.get("name") or "",
        "address": saved.get("address") or {},
        "timezone": saved.get("timezone") or "",
        "status": saved.get("status") or "",
        "geo": saved["geo"],
        "created": existing is None,
        "geocoded": geocoded,
    }


@api_router.post("/admin/locations", dependencies=[Depends(_provisioning_authorized)])
async def provision_location(body: LocationProvisioningRequest):
    """Provision one organization-owned canonical location for automation."""
    return await _provision_canonical_location(body)


def _valid_location_inventory_source(source: Any) -> bool:
    if not isinstance(source, dict):
        return False
    kind = source.get("kind")
    if kind in {"location_inventory", "menu_catalog"}:
        return True
    if kind != "user_cupboard":
        return False
    if not isinstance(source.get("user_id"), str) or not source["user_id"].strip():
        return False
    return "require_active" not in source or isinstance(source["require_active"], bool)


async def _rebuild_configured_location_drinks(location_id: str) -> dict[str, Any]:
    """Rebuild derived capabilities only for an active, configured location."""
    location = await db.locations.find_one(
        {"location_id": location_id}, {"_id": 0, "location_id": 1, "status": 1}
    )
    if not location or location.get("status") != "active":
        raise HTTPException(status_code=404, detail="Active location does not exist")

    settings = await db.location_settings.find_one(
        {"location_id": location_id}, {"_id": 0, "inventory_source": 1}
    )
    source = (settings or {}).get("inventory_source")
    if not _valid_location_inventory_source(source):
        raise HTTPException(status_code=409, detail="Location inventory source is not configured")

    generated_rows = await rebuild_location_drinks(location_id)
    logger.info(
        "Location drink rebuild completed location_id=%s generated_rows=%s",
        location_id,
        generated_rows,
    )
    return {"location_id": location_id, "generated_rows": generated_rows}


@api_router.post(
    "/admin/locations/{location_id}/rebuild-drinks",
    dependencies=[Depends(_location_inventory_rebuild_authorized)],
)
async def rebuild_configured_location_drinks(location_id: str):
    return await _rebuild_configured_location_drinks(location_id)


CHECK_IN_CONFIG_ID = "location_check_in"
CHECK_IN_RADIUS_FEET = 200
FEET_TO_METERS = 0.3048


class LocationCoordinatesRequest(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


async def _check_in_radius() -> tuple[float, float]:
    """Resolve the single canonical platform check-in radius from app_config."""
    doc = await db.app_config.find_one({"_id": CHECK_IN_CONFIG_ID}, {"_id": 0, "check_in_radius_feet": 1})
    if not doc or doc.get("check_in_radius_feet") is None:
        raise HTTPException(status_code=503, detail="Check-in proximity is not configured")
    feet = float(doc["check_in_radius_feet"])
    return feet, feet * FEET_TO_METERS


def _location_address_display(address: dict) -> str:
    address = address or {}
    street = " ".join(part for part in [address.get("line1"), address.get("line2")] if part)
    locality = ", ".join(part for part in [address.get("city"), address.get("state")] if part)
    if address.get("postal_code"):
        locality = f"{locality} {address['postal_code']}".strip()
    return ", ".join(part for part in [street, locality, address.get("country")] if part)


async def _closest_active_location(latitude: float, longitude: float) -> Optional[dict]:
    """One canonical geospatial source for discovery and check-in eligibility."""
    pipeline = [
        {
            "$geoNear": {
                "near": {"type": "Point", "coordinates": [longitude, latitude]},
                "key": "geo",
                "distanceField": "distance_meters",
                "spherical": True,
                "query": {"status": "active"},
            }
        },
        {"$limit": 1},
        {"$project": {"_id": 0}},
    ]
    rows = await db.locations.aggregate(pipeline).to_list(length=1)
    return rows[0] if rows else None


def _location_api_shape(doc: Optional[dict]) -> Optional[dict]:
    if not doc:
        return None
    geo = doc.get("geo") or {}
    coordinates = geo.get("coordinates") or []
    longitude = coordinates[0] if len(coordinates) >= 2 else None
    latitude = coordinates[1] if len(coordinates) >= 2 else None
    distance_meters = float(doc.get("distance_meters") or 0)
    return {
        "location_id": doc.get("location_id"),
        "name": doc.get("name") or "DrinkThink location",
        "address": doc.get("address") or {},
        "display_address": _location_address_display(doc.get("address") or {}),
        "distance_meters": round(distance_meters, 2),
        "distance_feet": round(distance_meters / FEET_TO_METERS, 2),
        "coordinates": {"latitude": latitude, "longitude": longitude},
    }


@api_router.post("/locations/closest")
async def closest_location(body: LocationCoordinatesRequest):
    """Return the closest active canonical location, regardless of check-in radius."""
    location = await _closest_active_location(body.latitude, body.longitude)
    return {"closest_location": _location_api_shape(location)}


@api_router.post("/locations/check-in-eligibility")
async def check_in_eligibility(body: LocationCoordinatesRequest):
    """Evaluate check-in eligibility using the same canonical closest-location query."""
    location = await _closest_active_location(body.latitude, body.longitude)
    radius_feet, radius_meters = await _check_in_radius()
    shaped = _location_api_shape(location)
    eligible = bool(location and float(location.get("distance_meters") or 0) <= radius_meters)
    return {
        "closest_location": shaped,
        "check_in_eligible": eligible,
        "eligible_location": shaped if eligible else None,
        "check_in_radius_feet": radius_feet,
        "check_in_radius_meters": radius_meters,
    }


class ShareCheckInRequest(BaseModel):
    location_id: str
    expires_at: datetime
    latitude: float
    longitude: float


# Transitional fallback for the current test locations. Canonical Mongo `locations`
# is always consulted first. Remove this fallback once both test locations have
# been migrated into the canonical collection.
SHAREABLE_LOCATIONS = {
    "loc_test_ties_house": {"location_id":"loc_test_ties_house","name":"Tie's house","address":"3417 S. Almeria Ave, Tampa, FL 33629"},
    "loc_test_forbici_tampa": {"location_id":"loc_test_forbici_tampa","name":"Forbici","address":"1633 W Snow Ave, Tampa, FL 33606"},
}


def _share_secret() -> bytes:
    # Production Share Check-In has its own secret. Do not fall back to an
    # administrative credential: these are separate security domains.
    secret = os.environ.get("SHARE_CHECKIN_SECRET")
    if not secret:
        raise HTTPException(status_code=503, detail="Shared check-in is not configured")
    return secret.encode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign_shared_checkin(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    body = _b64(raw)
    sig = _b64(hmac.new(_share_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def _verify_shared_checkin(token: str) -> dict:
    try:
        body, supplied = token.split(".", 1)
        expected = _b64(hmac.new(_share_secret(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(supplied, expected):
            raise ValueError()
        payload = json.loads(_unb64(body))
        if datetime.now(timezone.utc).timestamp() >= float(payload["exp"]):
            raise ValueError()
        return payload
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Shared check-in link is invalid or expired")


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _share_location(location_id: str) -> Optional[dict]:
    """Resolve a shareable location from the canonical collection first."""
    doc = await db.locations.find_one(
        {"location_id": location_id, "status": {"$ne": "inactive"}},
        {"_id": 0},
    )
    if doc:
        coordinates = (doc.get("geo") or {}).get("coordinates") or []
        return {
            "location_id": location_id,
            "name": doc.get("display_name") or doc.get("name") or "DrinkThink location",
            "address": doc.get("address"),
            "latitude": coordinates[1] if len(coordinates) >= 2 else None,
            "longitude": coordinates[0] if len(coordinates) >= 2 else None,
        }
    return SHAREABLE_LOCATIONS.get(location_id)


async def _new_share_id() -> str:
    # 12 URL-safe random characters provides an opaque, non-sequential public ID.
    # Retry on the extremely unlikely collision before the unique index is hit.
    for _ in range(5):
        candidate = "j_" + secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
        if not await db.share_checkins.find_one({"share_id": candidate}, {"_id": 1}):
            return candidate
    raise HTTPException(status_code=503, detail="Unable to create share invitation")


async def _share_record_or_error(share_id: str) -> dict:
    if not share_id or not share_id.startswith("j_"):
        raise HTTPException(status_code=404, detail="Share invitation not found")
    record = await db.share_checkins.find_one({"share_id": share_id}, {"_id": 0})
    if not record:
        raise HTTPException(status_code=404, detail="Share invitation not found")
    if record.get("status") != "active":
        raise HTTPException(status_code=410, detail="Share invitation is no longer available")
    expires = record.get("expires_at")
    if not isinstance(expires, datetime):
        raise HTTPException(status_code=503, detail="Share invitation service unavailable")
    expires = _as_aware(expires)
    if _now() >= expires:
        raise HTTPException(status_code=410, detail="Share invitation has expired")
    record["expires_at"] = expires
    return record


@api_router.post("/locations/share-check-in")
async def create_shared_checkin(body: ShareCheckInRequest, user: User = Depends(current_user)):
    loc = await _share_location(body.location_id)
    if not loc:
        raise HTTPException(status_code=404, detail="Location not available for sharing")

    now = _now()
    requested = _as_aware(body.expires_at)
    expires = min(requested, now + timedelta(hours=4))
    if expires <= now:
        raise HTTPException(status_code=400, detail="Check-in has expired")

    # Keep the existing signed security context private/server-side while the
    # opaque Share ID is the only identifier exposed in the public URL.
    token = _sign_shared_checkin({
        "location_id": body.location_id,
        "latitude": body.latitude,
        "longitude": body.longitude,
        "exp": int(expires.timestamp()),
        "source": "shared_link",
    })
    share_id = await _new_share_id()
    share_url = f"https://drinkthink.app/j/{share_id}"
    await db.share_checkins.insert_one({
        "share_id": share_id,
        "sharer_user_id": user.user_id,
        "location_id": body.location_id,
        "expires_at": expires,
        "status": "active",
        "source": "shared_link",
        "signed_token": token,
        "location_snapshot": {
            "display_name": loc.get("name") or "DrinkThink location",
            "address": loc.get("address"),
            "latitude": body.latitude,
            "longitude": body.longitude,
        },
        "created_at": now,
        "updated_at": now,
    })
    return {"share_id": share_id, "share_url": share_url, "expires_at": expires.isoformat()}


@api_router.get("/public/share-check-ins/{share_id}")
async def public_shared_checkin(share_id: str):
    record = await _share_record_or_error(share_id)
    loc = record.get("location_snapshot") or {}
    share_url = f"https://drinkthink.app/j/{share_id}"
    return {
        "share_id": share_id,
        "status": "active",
        "expires_at": record["expires_at"].isoformat(),
        "location": {
            "display_name": loc.get("display_name") or "DrinkThink location",
            "address": loc.get("address"),
            "latitude": loc.get("latitude"),
            "longitude": loc.get("longitude"),
        },
        "check_in": {
            "source": "shared_link",
            "deep_link": f"drinkthink://checkin?share_id={share_id}",
            "universal_link": share_url,
        },
    }


@api_router.get("/locations/shared-check-in")
async def validate_shared_checkin(token: Optional[str] = None, share_id: Optional[str] = None):
    """Resolve legacy signed-token links or the production opaque Share ID."""
    if share_id:
        record = await _share_record_or_error(share_id)
        # Verify the private signed security context as a second validation layer.
        payload = _verify_shared_checkin(record.get("signed_token", ""))
        if payload.get("location_id") != record.get("location_id"):
            raise HTTPException(status_code=410, detail="Share invitation is no longer available")
        loc = record.get("location_snapshot") or {}
        return {
            "location": {
                "location_id": record["location_id"],
                "name": loc.get("display_name") or "DrinkThink location",
                "address": loc.get("address"),
                "latitude": loc.get("latitude"),
                "longitude": loc.get("longitude"),
            },
            "source": "shared_link",
            "share_id": share_id,
            "expires_at": record["expires_at"].isoformat(),
        }

    if not token:
        raise HTTPException(status_code=400, detail="token or share_id is required")
    payload = _verify_shared_checkin(token)
    loc = await _share_location(payload.get("location_id"))
    if not loc:
        raise HTTPException(status_code=404, detail="Location not found")
    return {
        "location": {
            **loc,
            "latitude": payload["latitude"],
            "longitude": payload["longitude"],
        },
        "source": "shared_link",
        "expires_at": datetime.fromtimestamp(payload["exp"], timezone.utc).isoformat(),
    }


@api_router.delete("/locations/share-check-in/{share_id}")
async def revoke_shared_checkin(share_id: str, user: User = Depends(current_user)):
    """Explicitly revoke a Share Check-In owned by the authenticated sharer."""
    result = await db.share_checkins.update_one(
        {"share_id": share_id, "sharer_user_id": user.user_id, "status": "active"},
        {"$set": {"status": "revoked", "updated_at": _now()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Share invitation not found")
    return {"ok": True, "share_id": share_id, "status": "revoked"}


app.include_router(api_router)

_cors_origins = [
    "https://drinkthink.app",
    "https://www.drinkthink.app",
]
_extra_cors = os.environ.get("CORS_ORIGINS", "")
if _extra_cors:
    _cors_origins.extend(o.strip() for o in _extra_cors.split(",") if o.strip())

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=list(dict.fromkeys(_cors_origins)),
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)


@app.on_event("startup")
async def ensure_canonical_indexes():
    """Canonical knowledge data is migration-owned; startup never seeds legacy JSON."""
    await db.cocktails.create_index("cocktail_id", unique=True)
    await db.cocktails.create_index("normalized_name")
    await db.ingredients.create_index("ingredient_id", unique=True)
    await db.ingredients.create_index("parent_ingredient_id")
    await db.ingredient_categories.create_index("category_id", unique=True)
    await db.glasses.create_index("glass_id", unique=True)
    await db.organizations.create_index("organization_id", unique=True)
    await db.locations.create_index("location_id", unique=True)
    await db.locations.create_index("organization_id")
    await db.locations.create_index("status")
    await db.locations.create_index([("geo", "2dsphere")])
    await db.app_config.update_one(
        {"_id": CHECK_IN_CONFIG_ID},
        {"$setOnInsert": {"check_in_radius_feet": CHECK_IN_RADIUS_FEET, "created_at": _now()},
         "$set": {"updated_at": _now()}},
        upsert=True,
    )
    logger.info("Canonical knowledge/location indexes and global check-in configuration ensured")


@app.on_event("startup")
async def seed_auth_indexes():
    await db.users.create_index("email", unique=True)
    await db.users.create_index("user_id", unique=True)
    await db.user_sessions.create_index("session_token", unique=True)
    await db.user_sessions.create_index("user_id")
    # TTL: MongoDB auto-deletes sessions once expires_at is in the past.
    await db.user_sessions.create_index("expires_at", expireAfterSeconds=0)
    await db.share_checkins.create_index("share_id", unique=True)
    await db.share_checkins.create_index("sharer_user_id")
    await db.share_checkins.create_index("expires_at")
    await db.favorites.create_index(
        [("user_id", 1), ("drink_id", 1)], unique=True
    )
    await db.blocked.create_index(
        [("user_id", 1), ("drink_id", 1)], unique=True
    )
    await db.premium_entitlements.create_index([("platform", 1), ("purchase_identity_hash", 1)], unique=True)
    await db.premium_entitlements.create_index([("user_id", 1), ("platform", 1), ("product_id", 1)], unique=True)
    await db.premium_entitlements.create_index([("status", 1), ("last_verified_at", 1)])
    logger.info("Auth indexes ensured")


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
