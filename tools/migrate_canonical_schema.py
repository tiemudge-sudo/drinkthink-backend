#!/usr/bin/env python3
"""
DrinkThink additive MongoDB migration/backfill tool.

DRY-RUN IS THE DEFAULT.
Use --apply only after reviewing the generated report.

This tool never drops collections and never deletes production records.
"""
import argparse
import asyncio
import json
import os
import re
import secrets
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING


def now():
    return datetime.now(timezone.utc)


def normalize_name(value):
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def new_cocktail_id():
    return "ckt_" + secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]


def int_or_none(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def score_object(d):
    # Compatibility mapping locked in the migration contract.
    return {
        "strong": d.get("dark"),
        "fancy": d.get("fancy"),
        "comfort": d.get("calm"),
        "party": d.get("celebrate"),
        "thirsty": d.get("thirsty"),
    }


def canonical_cocktail(d, cocktail_id):
    return {
        "cocktail_id": cocktail_id,
        "legacy_drink_id": d["id"],
        "name": d.get("name", ""),
        "normalized_name": normalize_name(d.get("name")),
        "category": d.get("category") or None,
        "instructions": d.get("instructions") or None,
        "human_ingredients": d.get("ingredients") or None,
        "shopping_tokens": d.get("shopping") or None,
        "alcohol_class": d.get("alcohol") or None,
        # Deliberately unresolved here. Glass mapping must be explicit, not guessed.
        "glass_id": None,
        "legacy_glass": d.get("glass") or None,
        # Deliberately empty until canonical ingredient resolution exists.
        "main_ingredient_ids": [],
        "scores": score_object(d),
        "source": "curated",
        "admitted_from_review_id": None,
        "status": "active",
        "migration": {"source": "legacy_drinks", "version": "canonical-v1"},
    }


def walk_ingredient_tree(tree):
    """
    Supports the current Category -> primaries -> items representation.
    Emits candidate canonical records without guessing non-numeric ingredient IDs.
    """
    categories = []
    ingredients = []
    unresolved = []

    for c_idx, cat in enumerate(tree or []):
        cat_id = str(cat.get("id") or f"category_{c_idx+1}")
        categories.append({
            "category_id": cat_id,
            "name": cat.get("name") or cat_id,
            "display_order": c_idx,
            "status": "active",
        })
        for primary in cat.get("primaries", []) or []:
            pid = int_or_none(primary.get("id"))
            if pid is None:
                unresolved.append({
                    "kind": "ingredient_id",
                    "level": "primary",
                    "source_id": primary.get("id"),
                    "name": primary.get("name"),
                })
            else:
                ingredients.append({
                    "ingredient_id": pid,
                    "name": primary.get("name") or str(pid),
                    "normalized_name": normalize_name(primary.get("name")),
                    "category_id": cat_id,
                    "parent_ingredient_id": None,
                    "ingredient_type": "primary",
                    "aliases": [],
                    "status": "active",
                })

            for item in primary.get("items", []) or []:
                iid = int_or_none(item.get("id"))
                if iid is None:
                    unresolved.append({
                        "kind": "ingredient_id",
                        "level": "item",
                        "source_id": item.get("id"),
                        "name": item.get("name"),
                        "parent_source_id": primary.get("id"),
                    })
                    continue
                ingredients.append({
                    "ingredient_id": iid,
                    "name": item.get("name") or str(iid),
                    "normalized_name": normalize_name(item.get("name")),
                    "category_id": cat_id,
                    "parent_ingredient_id": pid,
                    "ingredient_type": "item",
                    "aliases": [],
                    "status": "active",
                })
    return categories, ingredients, unresolved


class Migration:
    def __init__(self, db, apply=False):
        self.db = db
        self.apply = apply
        self.report = {
            "mode": "APPLY" if apply else "DRY_RUN",
            "started_at": now().isoformat(),
            "counts": Counter(),
            "exceptions": [],
            "validation": {},
        }

    def count(self, key, n=1):
        self.report["counts"][key] += n

    async def ensure_indexes(self):
        specs = [
            ("cocktails", [("cocktail_id", ASCENDING)], {"unique": True}),
            ("cocktails", [("legacy_drink_id", ASCENDING)], {"unique": True, "sparse": True}),
            ("cocktails", [("normalized_name", ASCENDING)], {}),
            ("ingredient_categories", [("category_id", ASCENDING)], {"unique": True}),
            ("ingredients", [("ingredient_id", ASCENDING)], {"unique": True}),
            ("ingredients", [("parent_ingredient_id", ASCENDING)], {}),
            ("ingredients", [("category_id", ASCENDING)], {}),
            ("cocktail_ingredients", [("cocktail_id", ASCENDING)], {}),
            ("cocktail_ingredients", [("ingredient_id", ASCENDING)], {}),
            ("organizations", [("organization_id", ASCENDING)], {"unique": True}),
            ("locations", [("location_id", ASCENDING)], {"unique": True}),
            ("location_settings", [("location_id", ASCENDING)], {"unique": True}),
            ("location_inventory", [("location_id", ASCENDING), ("ingredient_id", ASCENDING)], {"unique": True}),
            ("location_drinks", [("location_id", ASCENDING), ("cocktail_id", ASCENDING)], {"unique": True}),
            ("pos_connections", [("pos_connection_id", ASCENDING)], {"unique": True}),
            ("pos_catalog_items", [("pos_connection_id", ASCENDING), ("provider_item_id", ASCENDING)], {"unique": True}),
            ("pos_ingredient_mappings", [("pos_catalog_item_id", ASCENDING), ("ingredient_id", ASCENDING)], {"unique": True}),
            ("pos_drink_mappings", [("mapping_id", ASCENDING)], {"unique": True}),
            ("pos_drink_mappings", [("pos_connection_id", ASCENDING), ("provider_drink_id", ASCENDING)], {"unique": True}),
            ("pos_sync_log", [("sync_id", ASCENDING)], {"unique": True}),
            ("drink_review_queue", [("review_id", ASCENDING)], {"unique": True}),
            ("orders", [("order_id", ASCENDING)], {"unique": True}),
            ("order_items", [("order_item_id", ASCENDING)], {"unique": True}),
            ("order_events", [("event_id", ASCENDING)], {"unique": True}),
            ("schema_migrations", [("migration_id", ASCENDING)], {"unique": True}),
        ]
        self.count("indexes_planned", len(specs))
        if not self.apply:
            return
        for collection, keys, options in specs:
            await self.db[collection].create_index(keys, **options)
            self.count("indexes_created")

    async def migrate_ingredients(self):
        doc = await self.db.ingredients_tree.find_one({"_id": "tree"}, {"_id": 0})
        if not doc:
            self.report["exceptions"].append({"kind": "missing_source", "collection": "ingredients_tree"})
            return
        categories, ingredients, unresolved = walk_ingredient_tree(doc.get("data", []))
        self.count("ingredient_categories_found", len(categories))
        self.count("canonical_ingredients_found", len(ingredients))
        self.report["exceptions"].extend(unresolved)
        self.count("ingredient_id_exceptions", len(unresolved))

        if self.apply:
            ts = now()
            for row in categories:
                row["updated_at"] = ts
                await self.db.ingredient_categories.update_one(
                    {"category_id": row["category_id"]},
                    {"$set": row, "$setOnInsert": {"created_at": ts}},
                    upsert=True,
                )
            for row in ingredients:
                row["updated_at"] = ts
                await self.db.ingredients.update_one(
                    {"ingredient_id": row["ingredient_id"]},
                    {"$set": row, "$setOnInsert": {"created_at": ts}},
                    upsert=True,
                )

    async def migrate_cocktails(self):
        seen = set()
        async for d in self.db.drinks.find({}, {"_id": 0}):
            legacy_id = d.get("id")
            if not isinstance(legacy_id, int):
                self.report["exceptions"].append({"kind": "invalid_legacy_drink_id", "value": legacy_id, "name": d.get("name")})
                continue
            if legacy_id in seen:
                self.report["exceptions"].append({"kind": "duplicate_legacy_drink_id", "value": legacy_id})
                continue
            seen.add(legacy_id)

            existing = await self.db.cocktails.find_one({"legacy_drink_id": legacy_id}, {"_id": 0, "cocktail_id": 1})
            cid = existing["cocktail_id"] if existing else new_cocktail_id()
            row = canonical_cocktail(d, cid)
            self.count("legacy_drinks_found")
            if existing:
                self.count("cocktails_existing")
            else:
                self.count("cocktails_to_create")

            if d.get("glass"):
                self.report["exceptions"].append({
                    "kind": "glass_mapping_required",
                    "legacy_drink_id": legacy_id,
                    "legacy_glass": d.get("glass"),
                })

            # Recipe rows are intentionally NOT guessed from free text.
            if d.get("ingredients") or d.get("shopping"):
                self.count("cocktails_requiring_recipe_resolution")

            if self.apply:
                ts = now()
                row["updated_at"] = ts
                await self.db.cocktails.update_one(
                    {"legacy_drink_id": legacy_id},
                    {"$set": row, "$setOnInsert": {"created_at": ts}},
                    upsert=True,
                )

    async def migrate_user_refs(self):
        for coll_name in ("favorites", "blocked", "pending_shares"):
            coll = self.db[coll_name]
            async for row in coll.find({}, {"_id": 1, "drink_id": 1, "cocktail_id": 1}):
                if row.get("cocktail_id"):
                    self.count(f"{coll_name}_already_canonical")
                    continue
                legacy_id = row.get("drink_id")
                cocktail = await self.db.cocktails.find_one(
                    {"legacy_drink_id": legacy_id}, {"_id": 0, "cocktail_id": 1}
                )
                # In dry-run, canonical collection may not exist yet. Resolve against legacy
                # and report the reference as migratable if the source drink exists.
                if not cocktail and not self.apply:
                    source = await self.db.drinks.find_one({"id": legacy_id}, {"_id": 0, "id": 1})
                    if source:
                        self.count(f"{coll_name}_references_migratable")
                        continue
                if not cocktail:
                    self.report["exceptions"].append({
                        "kind": "orphan_drink_reference",
                        "collection": coll_name,
                        "drink_id": legacy_id,
                    })
                    continue
                self.count(f"{coll_name}_references_to_backfill")
                if self.apply:
                    await coll.update_one({"_id": row["_id"]}, {"$set": {"cocktail_id": cocktail["cocktail_id"]}})

    async def validate_cupboard(self):
        valid_ids = set()
        async for row in self.db.ingredients.find({}, {"_id": 0, "ingredient_id": 1}):
            valid_ids.add(row["ingredient_id"])

        # Dry run can derive valid IDs from source tree.
        if not valid_ids and not self.apply:
            doc = await self.db.ingredients_tree.find_one({"_id": "tree"}, {"_id": 0})
            if doc:
                _, ingredients, _ = walk_ingredient_tree(doc.get("data", []))
                valid_ids = {x["ingredient_id"] for x in ingredients}

        async for row in self.db.user_cupboard.find({}, {"_id": 0, "user_id": 1, "item_ids": 1}):
            normalized = []
            bad = []
            for value in row.get("item_ids", []):
                iid = int_or_none(value)
                if iid is None or iid not in valid_ids:
                    bad.append(value)
                else:
                    normalized.append(iid)
            self.count("cupboard_users_checked")
            if bad:
                self.report["exceptions"].append({
                    "kind": "cupboard_unresolved_ids",
                    "user_id": row.get("user_id"),
                    "values": bad,
                })
            if self.apply and normalized != row.get("item_ids", []):
                await self.db.user_cupboard.update_one(
                    {"user_id": row["user_id"]}, {"$set": {"item_ids": normalized}}
                )
                self.count("cupboard_users_normalized")

    async def validate(self):
        legacy_count = await self.db.drinks.count_documents({})
        canonical_count = await self.db.cocktails.count_documents({})
        duplicate_legacy = []
        if self.apply:
            duplicate_legacy = await self.db.cocktails.aggregate([
                {"$match": {"legacy_drink_id": {"$ne": None}}},
                {"$group": {"_id": "$legacy_drink_id", "n": {"$sum": 1}}},
                {"$match": {"n": {"$gt": 1}}},
            ]).to_list(length=None)
        self.report["validation"] = {
            "legacy_drink_count": legacy_count,
            "canonical_cocktail_count": canonical_count,
            "canonical_count_expected_after_apply": legacy_count if not self.apply else canonical_count,
            "duplicate_canonical_legacy_ids": len(duplicate_legacy),
        }

    async def run(self):
        migration_id = "canonical_schema_v1"
        if self.apply:
            await self.db.schema_migrations.update_one(
                {"migration_id": migration_id},
                {"$set": {"version": 1, "status": "running", "started_at": now()}},
                upsert=True,
            )
        await self.ensure_indexes()
        await self.migrate_ingredients()
        await self.migrate_cocktails()
        await self.migrate_user_refs()
        await self.validate_cupboard()
        await self.validate()

        self.report["counts"] = dict(self.report["counts"])
        self.report["completed_at"] = now().isoformat()
        self.report["exception_count"] = len(self.report["exceptions"])

        if self.apply:
            await self.db.schema_migrations.update_one(
                {"migration_id": migration_id},
                {"$set": {
                    "status": "backfill_complete",
                    "completed_at": now(),
                    "counts": self.report["counts"],
                    "validation": self.report["validation"],
                    "exception_count": self.report["exception_count"],
                }},
            )
        return self.report


async def amain(args):
    load_dotenv(args.env_file)
    mongo_url = args.mongo_url or os.environ.get("MONGO_URL")
    db_name = args.db_name or os.environ.get("DB_NAME")
    if not mongo_url or not db_name:
        raise SystemExit("MONGO_URL and DB_NAME are required (environment, --mongo-url, or --db-name).")

    client = AsyncIOMotorClient(mongo_url)
    try:
        db = client[db_name]
        report = await Migration(db, apply=args.apply).run()
        out = Path(args.report)
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(json.dumps({
            "mode": report["mode"],
            "report": str(out),
            "counts": report["counts"],
            "exception_count": report["exception_count"],
            "validation": report["validation"],
        }, indent=2))
    finally:
        client.close()


def main():
    p = argparse.ArgumentParser(description="DrinkThink additive canonical MongoDB migration")
    p.add_argument("--apply", action="store_true", help="Perform additive upserts. Without this flag, tool is read-only.")
    p.add_argument("--env-file", default=".env")
    p.add_argument("--mongo-url")
    p.add_argument("--db-name")
    p.add_argument("--report", default="migration-report.json")
    args = p.parse_args()
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
