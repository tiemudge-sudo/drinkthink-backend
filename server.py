from fastapi import FastAPI, APIRouter, HTTPException, Header, Depends
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import json
import uuid
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional

import httpx


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

app = FastAPI()
api_router = APIRouter(prefix="/api")


# ------------- Models -------------
class Drink(BaseModel):
    id: int
    name: str
    category: str = ""
    alcohol: str = ""
    glass: str = ""
    ingredients: str = ""
    instructions: str = ""
    shopping: str = ""
    fancy: float
    dark: float
    thirsty: float
    calm: float
    celebrate: float


class MatchItem(BaseModel):
    drink: Drink
    score: float
    is_favorite: bool = False


class MatchResponse(BaseModel):
    results: List[MatchItem]
    scoring: str
    query: dict         # user slider labels -> values
    query_mapped: dict  # mapped to DB dims


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
    return {"message": "Drink Think API", "drinks": await db.drinks.count_documents({})}


@api_router.get("/drinks/count")
async def drinks_count():
    return {"count": await db.drinks.count_documents({})}


SPIRIT_KEYWORDS = {
    "vodka": ("vodka",),
    "gin": ("gin",),
    "rum": ("rum",),
    "whiskey": ("whiskey", "whisky", "bourbon", "scotch", "rye"),
    "tequila": ("tequila", "mezcal"),
}
ALLOWED_ALCOHOL_FILTERS = set(SPIRIT_KEYWORDS.keys()) | {"non_alcoholic"}

# Glass family → substrings that must appear in d_glass (lowercased).
GLASS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "cocktail":      ("cocktail glass", "margarita glass", "pina colada"),
    "highball":      ("highball", "collins", "cooler"),
    "old_fashioned": ("old-fashioned", "old fashioned", "whiskey sour",
                      "sour glass", "cordial", "brandy snifter"),
    "shot":          ("shot glass", "pousse cafe"),
    "hurricane":     ("hurricane", "parfait"),
    "wine":          ("wine glass", "wine goblet"),
    "champagne":     ("champagne",),
    "mug":           ("mug", "irish coffee", "mason jar", " cup", "coffee cup"),
}
ALLOWED_GLASS_FILTERS = set(GLASS_KEYWORDS.keys())


@api_router.get("/drinks/match", response_model=MatchResponse)
async def match_drink(
    strong: int, fancy: int, comfort: int, party: int, thirsty: int,
    scoring: str = "differential",
    limit: int = 5,
    alcohols: Optional[str] = None,
    glasses: Optional[str] = None,
    authorization: Optional[str] = Header(None),
):
    """Return the top matching drinks.

    scoring:
      * "differential" (default) — pure ratio-shape distance.
        Σ |user_ratio_i − drink_ratio_i|. All-5s == all-10s.
      * "alternate" — differential + |drink_sum − user_sum| / user_sum.
        Adds a magnitude penalty; all-5s ≠ all-10s.

    alcohols: comma-separated list from
      {vodka, gin, rum, whiskey, tequila, non_alcoholic}. OR-combined.
      Omit / empty → no filter.

    glasses: comma-separated list from
      {cocktail, highball, old_fashioned, shot, hurricane, wine, champagne, mug}.
      OR-combined. Applied on top of alcohols (both must match).

    When authenticated (Bearer token, optional):
      * Blocked drinks are excluded from results.
      * The best-scoring favorite drink is guaranteed to appear at position
        `limit` (last), demoting the last regular match if needed.
    """
    if scoring not in ("differential", "alternate"):
        raise HTTPException(status_code=400,
                            detail="scoring must be 'differential' or 'alternate'")
    if limit < 1 or limit > 50:
        raise HTTPException(status_code=400, detail="limit must be 1..50")

    # Optional auth — silent-fail to anonymous
    me: Optional[User] = None
    if authorization:
        try:
            me = await current_user(authorization)
        except HTTPException:
            me = None

    slider_vals = {"strong": strong, "fancy": fancy, "comfort": comfort,
                   "party": party, "thirsty": thirsty}
    for name, v in slider_vals.items():
        if not isinstance(v, int) or v < 1 or v > 10:
            raise HTTPException(status_code=400,
                                detail=f"{name} must be an integer between 1 and 10")

    alcohol_filters: set[str] = set()
    if alcohols:
        alcohol_filters = {a.strip().lower() for a in alcohols.split(",") if a.strip()}
        unknown = alcohol_filters - ALLOWED_ALCOHOL_FILTERS
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown alcohol filters: {sorted(unknown)}",
            )

    glass_filters: set[str] = set()
    if glasses:
        glass_filters = {g.strip().lower() for g in glasses.split(",") if g.strip()}
        unknown_g = glass_filters - ALLOWED_GLASS_FILTERS
        if unknown_g:
            raise HTTPException(
                status_code=400,
                detail=f"unknown glass filters: {sorted(unknown_g)}",
            )

    dim_vals = {SLIDER_TO_DIM[k]: v for k, v in slider_vals.items()}
    user_sum = sum(dim_vals.values())
    user_ratios = {d: dim_vals[d] / user_sum for d in DIMS}

    docs = await db.drinks.find({}, {"_id": 0}).to_list(length=None)
    if not docs:
        raise HTTPException(status_code=404, detail="No drinks in database")

    def matches_filter(doc: dict) -> bool:
        # Alcohol filter (OR within group)
        if alcohol_filters:
            want_nonalc = "non_alcoholic" in alcohol_filters
            spirit_keys = alcohol_filters - {"non_alcoholic"}
            ok = False
            if want_nonalc and doc.get("alcohol", "").lower().startswith("non"):
                ok = True
            if not ok and spirit_keys:
                txt = (doc.get("ingredients", "") + " " +
                       doc.get("shopping", "")).lower()
                for sp in spirit_keys:
                    if any(kw in txt for kw in SPIRIT_KEYWORDS[sp]):
                        ok = True
                        break
            if not ok:
                return False

        # Glass filter (OR within group)
        if glass_filters:
            gtxt = doc.get("glass", "").lower()
            if not any(
                any(kw in gtxt for kw in GLASS_KEYWORDS[g])
                for g in glass_filters
            ):
                return False

        return True

    filtered = [d for d in docs if matches_filter(d)]

    # Auth-aware filtering: exclude blocked
    blocked_ids: set[int] = set()
    favorite_ids: set[int] = set()
    if me:
        blocked_ids = {
            b["drink_id"]
            async for b in db.blocked.find({"user_id": me.user_id}, {"_id": 0, "drink_id": 1})
        }
        favorite_ids = {
            f["drink_id"]
            async for f in db.favorites.find({"user_id": me.user_id}, {"_id": 0, "drink_id": 1})
        }
    if blocked_ids:
        filtered = [d for d in filtered if d["id"] not in blocked_ids]

    def score_of(doc: dict) -> float:
        s = doc["fancy"] + doc["dark"] + doc["thirsty"] + doc["calm"] + doc["celebrate"]
        if s <= 0:
            return float("inf")
        diff = sum(abs(user_ratios[d] - doc[d] / s) for d in DIMS)
        if scoring == "alternate":
            diff += abs(s - user_sum) / user_sum
        return diff

    scored_all = sorted(
        ((d, score_of(d)) for d in filtered),
        key=lambda x: x[1],
    )
    top = scored_all[:limit]

    # Feature 3+4: pin best-matching favorite at position `limit` (last)
    # if not already present in the top slice.
    if favorite_ids and top:
        top_ids = {d["id"] for d, _ in top}
        need_pin = not (favorite_ids & top_ids)
        if need_pin:
            best_fav_pair = next(
                ((d, s) for d, s in scored_all if d["id"] in favorite_ids),
                None,
            )
            if best_fav_pair:
                # Demote the last slot for the favorite tail.
                top = top[: max(0, limit - 1)] + [best_fav_pair]

    results = [
        MatchItem(
            drink=Drink(**d),
            score=float(s),
            is_favorite=d["id"] in favorite_ids,
        )
        for d, s in top
    ]

    return MatchResponse(
        results=results,
        scoring=scoring,
        query=slider_vals,
        query_mapped=dim_vals,
    )


@api_router.get("/drinks/{drink_id}", response_model=Drink)
async def get_drink(drink_id: int):
    doc = await db.drinks.find_one({"id": drink_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Drink not found")
    return Drink(**doc)


# ================================================================
#                     AUTH — Emergent Google OAuth
# ================================================================

EMERGENT_SESSION_DATA_URL = "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data"
SESSION_TTL_DAYS = 7


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def current_user(authorization: Optional[str] = Header(None)) -> User:
    """Resolve the bearer token into a User. 401s on any failure."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    session = await db.user_sessions.find_one({"session_token": token}, {"_id": 0})
    if not session:
        raise HTTPException(status_code=401, detail="Invalid session")
    # Normalize expires_at to aware
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


# ================================================================
#                  ME — Favorites & Blocked drinks
# ================================================================

class DrinkStatus(BaseModel):
    drink_id: int
    is_favorite: bool
    is_blocked: bool


async def _drink_exists_or_404(drink_id: int) -> None:
    if not await db.drinks.find_one({"id": drink_id}, {"_id": 0, "id": 1}):
        raise HTTPException(status_code=404, detail="Drink not found")


@api_router.get("/me/status/{drink_id}", response_model=DrinkStatus)
async def me_status(drink_id: int, user: User = Depends(current_user)):
    await _drink_exists_or_404(drink_id)
    fav = await db.favorites.find_one(
        {"user_id": user.user_id, "drink_id": drink_id}, {"_id": 0}
    )
    blk = await db.blocked.find_one(
        {"user_id": user.user_id, "drink_id": drink_id}, {"_id": 0}
    )
    return DrinkStatus(drink_id=drink_id, is_favorite=bool(fav), is_blocked=bool(blk))


@api_router.post("/me/favorites/{drink_id}")
async def add_favorite(drink_id: int, user: User = Depends(current_user)):
    await _drink_exists_or_404(drink_id)
    # Adding a favorite un-blocks it (mutually exclusive states).
    await db.blocked.delete_one({"user_id": user.user_id, "drink_id": drink_id})
    await db.favorites.update_one(
        {"user_id": user.user_id, "drink_id": drink_id},
        {"$setOnInsert": {"created_at": _now()}},
        upsert=True,
    )
    return {"ok": True}


@api_router.delete("/me/favorites/{drink_id}")
async def remove_favorite(drink_id: int, user: User = Depends(current_user)):
    await db.favorites.delete_one({"user_id": user.user_id, "drink_id": drink_id})
    return {"ok": True}


@api_router.get("/me/favorites", response_model=List[Drink])
async def list_favorites(user: User = Depends(current_user)):
    rows = await db.favorites.find(
        {"user_id": user.user_id}, {"_id": 0}
    ).sort("created_at", -1).to_list(length=None)
    if not rows:
        return []
    ids = [r["drink_id"] for r in rows]
    drinks_by_id = {
        d["id"]: d
        async for d in db.drinks.find({"id": {"$in": ids}}, {"_id": 0})
    }
    return [Drink(**drinks_by_id[i]) for i in ids if i in drinks_by_id]


@api_router.post("/me/blocked/{drink_id}")
async def add_block(drink_id: int, user: User = Depends(current_user)):
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
async def remove_block(drink_id: int, user: User = Depends(current_user)):
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
    drinks_by_id = {
        d["id"]: d
        async for d in db.drinks.find({"id": {"$in": ids}}, {"_id": 0})
    }
    return [Drink(**drinks_by_id[i]) for i in ids if i in drinks_by_id]


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)


@app.on_event("startup")
async def seed_db():
    """Load the Drink Think DB into MongoDB on cold start.

    Re-seeds only when the count doesn't match the JSON row count, so schema
    tweaks or fresh dumps automatically propagate.
    """
    data_path = ROOT_DIR / "data" / "drinks.json"
    if not data_path.exists():
        logger.warning("drinks.json missing; skipping seed")
        return
    with open(data_path) as f:
        drinks = json.load(f)
    have = await db.drinks.count_documents({})
    if have == len(drinks):
        logger.info(f"drinks collection already seeded: {have}")
        return
    logger.info(f"Reseeding drinks: had {have}, loading {len(drinks)}")
    await db.drinks.drop()
    # Insert in chunks to avoid huge single-op
    CHUNK = 2000
    for i in range(0, len(drinks), CHUNK):
        await db.drinks.insert_many(drinks[i:i + CHUNK])
    # Index by id for future lookups
    await db.drinks.create_index("id", unique=True)
    logger.info(f"Seeded {await db.drinks.count_documents({})} drinks")


@app.on_event("startup")
async def seed_auth_indexes():
    await db.users.create_index("email", unique=True)
    await db.users.create_index("user_id", unique=True)
    await db.user_sessions.create_index("session_token", unique=True)
    await db.user_sessions.create_index("user_id")
    # TTL: MongoDB auto-deletes sessions once expires_at is in the past.
    await db.user_sessions.create_index("expires_at", expireAfterSeconds=0)
    await db.favorites.create_index(
        [("user_id", 1), ("drink_id", 1)], unique=True
    )
    await db.blocked.create_index(
        [("user_id", 1), ("drink_id", 1)], unique=True
    )
    logger.info("Auth indexes ensured")


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
