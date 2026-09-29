#!/usr/bin/env python3
"""Rebuild canonical cocktail glass IDs from the master d_glass_category field.

This migration deliberately does not read legacy d_glass values (or any other
glass-type string) to determine cocktails.glass_id.  It uses the established
legacy_drink_id bridge, validates the target canonical glasses records, and is
dry-run by default.
"""
import argparse
import asyncio
import csv
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient


VERSION = "rebuild-cocktail-glass-ids-v1"
CONFIRM = "REBUILD_COCKTAIL_GLASS_IDS_V1"
ROOT = Path(__file__).resolve().parents[1]

# Locked d_glass_category -> canonical glass_id mapping.  Do not infer values
# from d_glass, cocktails.migration.legacy_glass, or display strings.
GLASS_CATEGORY_TO_ID = {
    "Champagne flute": "champagne_flute",
    "Cordial glass": "cordial",
    "Highball glass": "highball",
    "Margarita glass": "margarita",
    "Martini glass": "martini",
    "Mug": "mug",
    "Pint glass": "pint",
    "Rocks glass": "rocks",
    "Shot glass": "shot",
    "Wine glass": "wine",
}


def now():
    return datetime.now(timezone.utc)


class Migration:
    def __init__(self, db, apply: bool):
        self.db = db
        self.apply = apply
        self.report = {
            "version": VERSION,
            "mode": "APPLY" if apply else "DRY_RUN",
            "started_at": now().isoformat(),
            "source": "data/drinks_clean_100pct.csv:d_glass_category",
            "mapping": GLASS_CATEGORY_TO_ID,
            "counts": Counter(),
            "exceptions": [],
            "glass_metadata_audit": {},
        }

    async def plan(self):
        with (ROOT / "data" / "drinks_clean_100pct.csv").open(encoding="utf-8-sig", newline="") as source:
            master_rows = list(csv.DictReader(source))

        by_legacy = defaultdict(list)
        async for cocktail in self.db.cocktails.find(
            {}, {"_id": 0, "cocktail_id": 1, "legacy_drink_id": 1, "migration.legacy_drink_id": 1}
        ):
            legacy_id = cocktail.get("legacy_drink_id")
            if legacy_id is None:
                legacy_id = (cocktail.get("migration") or {}).get("legacy_drink_id")
            try:
                by_legacy[int(legacy_id)].append(cocktail["cocktail_id"])
            except (KeyError, TypeError, ValueError):
                continue

        glasses = {
            glass["glass_id"]: glass
            async for glass in self.db.glasses.find(
                {"status": "active"}, {"_id": 0, "glass_id": 1, "filter_families": 1, "icon_key": 1}
            )
            if glass.get("glass_id")
        }
        missing_glasses = sorted(set(GLASS_CATEGORY_TO_ID.values()) - set(glasses))
        if missing_glasses:
            self.report["exceptions"].append({"kind": "missing_active_glass_records", "glass_ids": missing_glasses})

        for glass_id in sorted(GLASS_CATEGORY_TO_ID.values()):
            glass = glasses.get(glass_id)
            if glass:
                self.report["glass_metadata_audit"][glass_id] = {
                    "filter_families": glass.get("filter_families") or [],
                    "icon_key": glass.get("icon_key") or "",
                }

        updates = []
        category_counts = Counter()
        for row in master_rows:
            try:
                legacy_id = int(row["id"])
            except (KeyError, TypeError, ValueError):
                self.report["exceptions"].append({"kind": "invalid_master_id", "row": row})
                continue
            category = (row.get("d_glass_category") or "").strip()
            glass_id = GLASS_CATEGORY_TO_ID.get(category)
            if not glass_id:
                self.report["exceptions"].append(
                    {"kind": "unmapped_glass_category", "legacy_drink_id": legacy_id, "d_glass_category": category}
                )
                continue
            candidates = by_legacy.get(legacy_id, [])
            if len(candidates) != 1:
                self.report["exceptions"].append(
                    {"kind": "cocktail_bridge_count", "legacy_drink_id": legacy_id, "count": len(candidates), "cocktail_ids": candidates}
                )
                continue
            category_counts[category] += 1
            updates.append({"legacy_drink_id": legacy_id, "cocktail_id": candidates[0], "glass_id": glass_id})

        self.updates = updates
        self.report["counts"].update(
            {"master_rows": len(master_rows), "planned_cocktail_updates": len(updates), "mapped_categories": len(category_counts)}
        )
        self.report["category_counts"] = dict(sorted(category_counts.items()))
        self.report["validation"] = {
            "all_10_locked_categories_present": set(category_counts) == set(GLASS_CATEGORY_TO_ID),
            "all_master_rows_bridged_and_mapped": len(updates) == len(master_rows),
            "all_target_glasses_active": not missing_glasses,
            "exception_count": len(self.report["exceptions"]),
        }

    async def apply_updates(self):
        timestamp = now()
        for update in self.updates:
            await self.db.cocktails.update_one(
                {"cocktail_id": update["cocktail_id"]},
                {"$set": {"glass_id": update["glass_id"], "updated_at": timestamp}},
            )
        self.report["post_apply"] = {
            "cocktails_with_each_glass_id": {
                glass_id: await self.db.cocktails.count_documents({"glass_id": glass_id})
                for glass_id in sorted(GLASS_CATEGORY_TO_ID.values())
            }
        }

    async def run(self):
        await self.plan()
        validation = self.report["validation"]
        valid = (
            validation["all_10_locked_categories_present"]
            and validation["all_master_rows_bridged_and_mapped"]
            and validation["all_target_glasses_active"]
            and validation["exception_count"] == 0
        )
        if self.apply:
            if not valid:
                raise RuntimeError("APPLY BLOCKED: bridge, category, or canonical glasses validation failed")
            await self.apply_updates()
        self.report["counts"] = dict(self.report["counts"])
        self.report["finished_at"] = now().isoformat()
        return self.report


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-apply")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--report", default="cocktail-glass-id-rebuild-report.json")
    args = parser.parse_args()
    if args.apply and args.confirm_apply != CONFIRM:
        raise SystemExit(f"--apply requires --confirm-apply {CONFIRM}")

    load_dotenv(ROOT / args.env_file)
    mongo_url = os.getenv("MONGO_URL")
    db_name = os.getenv("DB_NAME")
    if not mongo_url or not db_name:
        raise SystemExit("MONGO_URL and DB_NAME are required")

    client = AsyncIOMotorClient(mongo_url)
    try:
        report = await Migration(client[db_name], args.apply).run()
    finally:
        client.close()
    Path(args.report).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"report": args.report, "validation": report["validation"], "counts": report["counts"]}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
