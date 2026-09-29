#!/usr/bin/env python3
"""Audit and repair canonical DrinkThink glass data from d_glass_category only."""
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

VERSION = "rebuild-cocktail-glass-ids-v2"
REPAIR_CONFIRM = "REPAIR_CANONICAL_GLASSES_V2"
APPLY_CONFIRM = "REBUILD_COCKTAIL_GLASS_IDS_V2"
ROOT = Path(__file__).resolve().parents[1]

# Locked master d_glass_category -> canonical glass_id mapping. Never infer
# assignment from d_glass, cocktail names, recipes, ingredients, or old IDs.
GLASS_SPECS = {
    "Champagne flute": {"glass_id": "champagne_flute", "filter_families": ["champagne_flutes"], "icon_key": "champagne_flute"},
    "Cordial glass": {"glass_id": "cordial_glass", "filter_families": ["hurricane"], "icon_key": "cordial"},
    "Highball glass": {"glass_id": "highball_glass", "filter_families": ["pint", "hurricane"], "icon_key": "highball"},
    "Margarita glass": {"glass_id": "margarita_glass", "filter_families": ["hurricane", "martini"], "icon_key": "margarita"},
    "Martini glass": {"glass_id": "martini_glass", "filter_families": ["martini"], "icon_key": "martini"},
    "Mug": {"glass_id": "mug", "filter_families": ["pint", "rocks"], "icon_key": "mug"},
    "Pint glass": {"glass_id": "pint_glass", "filter_families": ["pint"], "icon_key": "pint"},
    "Rocks glass": {"glass_id": "rocks_glass", "filter_families": ["rocks"], "icon_key": "rocks"},
    "Shot glass": {"glass_id": "shot_glass", "filter_families": ["shot"], "icon_key": "shot"},
    "Wine glass": {"glass_id": "wine_glass", "filter_families": ["hurricane", "champagne_flutes"], "icon_key": "wine"},
}
VALID_FILTER_FAMILIES = {family for spec in GLASS_SPECS.values() for family in spec["filter_families"]}
SPECS_BY_ID = {spec["glass_id"]: spec for spec in GLASS_SPECS.values()}


def now():
    return datetime.now(timezone.utc)


class GlassMigration:
    def __init__(self, db, repair_glasses: bool, apply: bool):
        self.db, self.repair_glasses, self.apply = db, repair_glasses, apply
        self.report = {
            "version": VERSION, "mode": "APPLY" if apply else "DRY_RUN",
            "started_at": now().isoformat(),
            "source": "data/drinks_clean_100pct.csv:d_glass_category",
            "locked_glass_specs": GLASS_SPECS, "counts": Counter(), "exceptions": [],
        }

    async def repair_metadata(self):
        timestamp = now()
        changes = []
        for glass_id, spec in SPECS_BY_ID.items():
            previous = await self.db.glasses.find_one({"glass_id": glass_id}, {"_id": 0})
            await self.db.glasses.update_one(
                {"glass_id": glass_id},
                {"$set": {"filter_families": spec["filter_families"], "icon_key": spec["icon_key"], "status": "active", "updated_at": timestamp},
                 "$setOnInsert": {"name": glass_id.replace("_", " ").title(), "display_name": glass_id.replace("_", " ").title(), "created_at": timestamp}},
                upsert=True,
            )
            changes.append({"glass_id": glass_id, "created": previous is None, "metadata_changed": previous is None or previous.get("filter_families") != spec["filter_families"] or previous.get("icon_key") != spec["icon_key"]})
        self.report["glasses_metadata_changes"] = changes

    async def audit_glasses(self):
        docs = {doc["glass_id"]: doc async for doc in self.db.glasses.find({}, {"_id": 0, "glass_id": 1, "status": 1, "filter_families": 1, "icon_key": 1}) if doc.get("glass_id")}
        audit, errors = {}, []
        for glass_id, spec in SPECS_BY_ID.items():
            actual = docs.get(glass_id)
            audit[glass_id] = {"expected": spec, "actual": actual}
            if not actual:
                errors.append({"kind": "missing_canonical_glass", "glass_id": glass_id})
                continue
            if actual.get("status") != "active": errors.append({"kind": "inactive_canonical_glass", "glass_id": glass_id})
            if actual.get("filter_families") != spec["filter_families"]: errors.append({"kind": "filter_families_mismatch", "glass_id": glass_id, "actual": actual.get("filter_families")})
            if actual.get("icon_key") != spec["icon_key"]: errors.append({"kind": "icon_key_mismatch", "glass_id": glass_id, "actual": actual.get("icon_key")})
            invalid = set(actual.get("filter_families") or []) - VALID_FILTER_FAMILIES
            if invalid: errors.append({"kind": "invalid_filter_family", "glass_id": glass_id, "values": sorted(invalid)})
        self.report["canonical_glasses_audit"] = audit
        self.report["canonical_glasses_errors"] = errors
        self.report["canonical_glasses_audit_passed"] = not errors

    async def plan_assignments(self):
        with (ROOT / "data" / "drinks_clean_100pct.csv").open(encoding="utf-8-sig", newline="") as source:
            master = list(csv.DictReader(source))
        by_legacy, production_distribution = defaultdict(list), Counter()
        async for cocktail in self.db.cocktails.find({}, {"_id": 0, "cocktail_id": 1, "legacy_drink_id": 1, "migration.legacy_drink_id": 1, "glass_id": 1}):
            production_distribution[str(cocktail.get("glass_id") or "<missing>")] += 1
            legacy_id = cocktail.get("legacy_drink_id")
            if legacy_id is None:
                legacy_id = (cocktail.get("migration") or {}).get("legacy_drink_id")
            try: by_legacy[int(legacy_id)].append(cocktail)
            except (TypeError, ValueError): pass

        updates, mismatches, category_counts, expected_distribution, actual_distribution = [], [], Counter(), Counter(), Counter()
        representatives, pina_colada = {}, None
        for row in master:
            try: legacy_id = int(row["id"])
            except (KeyError, TypeError, ValueError):
                self.report["exceptions"].append({"kind": "invalid_master_id", "row": row}); continue
            category = (row.get("d_glass_category") or "").strip()
            spec = GLASS_SPECS.get(category)
            if not spec:
                self.report["exceptions"].append({"kind": "unmapped_glass_category", "legacy_drink_id": legacy_id, "d_glass_category": category}); continue
            candidates = by_legacy.get(legacy_id, [])
            if len(candidates) != 1:
                self.report["exceptions"].append({"kind": "cocktail_bridge_count", "legacy_drink_id": legacy_id, "count": len(candidates), "cocktail_ids": [c.get("cocktail_id") for c in candidates]}); continue
            cocktail, expected = candidates[0], spec["glass_id"]
            actual = cocktail.get("glass_id")
            category_counts[category] += 1; expected_distribution[expected] += 1; actual_distribution[str(actual or "<missing>")] += 1
            record = {"legacy_drink_id": legacy_id, "cocktail_id": cocktail["cocktail_id"], "d_glass_category": category, "expected_glass_id": expected, "actual_glass_id": actual}
            if actual != expected: mismatches.append(record)
            updates.append(record)
            representatives.setdefault(expected, record)
            name = (row.get("d_name") or "").casefold()
            if "pina colada" in name or "piña colada" in name: pina_colada = record

        self.updates = updates
        self.report["counts"].update({"master_rows": len(master), "planned_cocktail_updates": len(updates), "mapped_categories": len(category_counts)})
        self.report["production_glass_id_distribution"] = dict(sorted(production_distribution.items()))
        self.report["bridged_assignment_audit"] = {"expected_glass_id_distribution": dict(sorted(expected_distribution.items())), "actual_glass_id_distribution": dict(sorted(actual_distribution.items())), "mismatch_count": len(mismatches), "missing_glass_id_count": sum(1 for row in updates if not row["actual_glass_id"]), "mismatch_samples": mismatches[:100]}
        self.report["representative_cocktails"] = {glass_id: record for glass_id, record in sorted(representatives.items())}
        self.report["pina_colada"] = pina_colada
        if set(category_counts) != set(GLASS_SPECS): self.report["exceptions"].append({"kind": "locked_categories_not_all_present"})
        if len(updates) != len(master): self.report["exceptions"].append({"kind": "not_all_master_rows_joined_and_mapped"})

    async def apply_assignments(self):
        changed, timestamp = 0, now()
        for update in self.updates:
            if update["actual_glass_id"] != update["expected_glass_id"]:
                result = await self.db.cocktails.update_one({"cocktail_id": update["cocktail_id"]}, {"$set": {"glass_id": update["expected_glass_id"], "updated_at": timestamp}})
                changed += result.modified_count
        self.report["cocktails_glass_id_records_changed"] = changed

    async def resolve_verification(self):
        glasses = {doc["glass_id"]: doc async for doc in self.db.glasses.find({}, {"_id": 0, "glass_id": 1, "filter_families": 1, "icon_key": 1}) if doc.get("glass_id")}
        items = list(self.report["representative_cocktails"].values())
        if self.report["pina_colada"] and self.report["pina_colada"] not in items: items.append(self.report["pina_colada"])
        self.report["metadata_resolution_verification"] = [{**item, "resolved_glass": glasses.get(item["expected_glass_id"])} for item in items]

    async def run(self):
        if self.repair_glasses: await self.repair_metadata()
        await self.audit_glasses()
        if self.apply and not self.report["canonical_glasses_audit_passed"]:
            raise RuntimeError("APPLY BLOCKED: canonical glasses metadata audit failed")
        await self.plan_assignments()
        if self.apply:
            if self.report["exceptions"]: raise RuntimeError("APPLY BLOCKED: master mapping or cocktail bridge audit failed")
            await self.apply_assignments()
            await self.audit_glasses()
            await self.plan_assignments()
            assignment_audit = self.report["bridged_assignment_audit"]
            self.report["post_apply_acceptance_passed"] = (
                self.report["canonical_glasses_audit_passed"]
                and assignment_audit["mismatch_count"] == 0
                and assignment_audit["missing_glass_id_count"] == 0
            )
        await self.resolve_verification()
        self.report["counts"] = dict(self.report["counts"])
        self.report["finished_at"] = now().isoformat()
        return self.report


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repair-glasses", action="store_true")
    parser.add_argument("--confirm-repair")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-apply")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--report", default="cocktail-glass-id-audit.json")
    args = parser.parse_args()
    if args.repair_glasses and args.confirm_repair != REPAIR_CONFIRM: raise SystemExit(f"--repair-glasses requires --confirm-repair {REPAIR_CONFIRM}")
    if args.apply and args.confirm_apply != APPLY_CONFIRM: raise SystemExit(f"--apply requires --confirm-apply {APPLY_CONFIRM}")
    load_dotenv(ROOT / args.env_file)
    mongo_url, db_name = os.getenv("MONGO_URL"), os.getenv("DB_NAME")
    if not mongo_url or not db_name: raise SystemExit("MONGO_URL and DB_NAME are required")
    client = AsyncIOMotorClient(mongo_url)
    try: report = await GlassMigration(client[db_name], args.repair_glasses, args.apply).run()
    finally: client.close()
    Path(args.report).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"report": args.report, "canonical_glasses_audit_passed": report["canonical_glasses_audit_passed"], "assignment_audit": report["bridged_assignment_audit"], "records_changed": report.get("cocktails_glass_id_records_changed", 0)}, indent=2))


if __name__ == "__main__": asyncio.run(main())
