#!/usr/bin/env python3
"""Read-only DrinkThink v3.2 retired-ingredient-ID audit.

This tool only calls MongoDB read operations (list, count, find, aggregate).
It never creates indexes or writes data.  Reports contain aggregate counts and
retired IDs only; they deliberately omit document IDs and user information.
"""
import argparse
import asyncio
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_DB = "DrinkThinkv0"
MERGES = "ingredient_id_merges"
INGREDIENTS = "ingredients"
# These rows are always present in the report, including when a collection is
# absent or contains no retired references.
NAMED_COLLECTIONS = (
    "cocktail_ingredients", "cocktails", "user_cupboard", "location_inventory",
    "ingredient_availability", "inventory", "vendor_ingredient_mappings",
    "pos_ingredient_mappings", "admissions", "reviews", "recipe_generations",
    "staging", "ingredient_id_merges", "ingredients",
)


def now():
    return datetime.now(timezone.utc).isoformat()


def identity(value):
    """Normalize numeric/string ingredient identifiers without fuzzy matching."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return None


def sort_ids(values):
    return sorted(values, key=lambda value: (not str(value).isdigit(), int(value) if str(value).isdigit() else str(value)))


def classification(collection):
    name = collection.lower()
    if "user" in name or name in {"favorites", "blocked", "pending_shares"}:
        return "user operational data"
    if any(token in name for token in ("location", "inventory", "vendor", "pos", "availability")):
        return "location/vendor/POS operational data"
    if any(token in name for token in ("staging", "stage", "review", "admission", "generated", "generation", "import", "draft")):
        return "staging/review/generated data"
    if any(token in name for token in ("history", "audit", "log", "archive", "legacy", "event")):
        return "historical/audit-only data"
    # Unknown live collections are conservatively treated as canonical rather
    # than silently classifying operational references as historical.
    return "canonical master/relationship data"


def ingredient_key(key, collection):
    key = key.lower()
    if key in {"ingredient_id", "ingredient_ids", "main_ingredient_ids", "required_ingredient_ids",
               "resolved_to_id", "to_id", "from_id"}:
        return True
    # user_cupboard's canonical persisted ingredient array has this generic name.
    if collection == "user_cupboard" and key in {"item_id", "item_ids"}:
        return True
    return "ingredient" in key and (key.endswith("_id") or key.endswith("_ids"))


def scalar_ids(value):
    """Yield scalar values from an ID field; never inspect arbitrary text fields."""
    if isinstance(value, list):
        for item in value:
            yield from scalar_ids(item)
    elif isinstance(value, dict):
        # Some operational payloads store typed ID arrays or a wrapper object.
        for item in value.values():
            yield from scalar_ids(item)
    else:
        normalized = identity(value)
        if normalized is not None:
            yield normalized


def retired_values_in_document(document, collection, retired_ids):
    """Return {field_path: [retired_id, ...]} without retaining document data."""
    found = defaultdict(list)

    def walk(value, path=""):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "_id":
                    continue
                child_path = f"{path}.{key}" if path else key
                if ingredient_key(key, collection):
                    found[child_path].extend(item for item in scalar_ids(child) if item in retired_ids)
                elif isinstance(child, (dict, list)):
                    walk(child, child_path)
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, (dict, list)):
                    walk(child, f"{path}[]")

    walk(document)
    return {path: values for path, values in found.items() if values}


async def active_ingredient_ids(db):
    values = set()
    async for row in db[INGREDIENTS].find({"status": "active"}, {"_id": 0, "ingredient_id": 1}):
        value = identity(row.get("ingredient_id"))
        if value is not None:
            values.add(value)
    return values


async def merge_integrity(db, exists):
    if MERGES not in exists:
        return {"collection_exists": False}, set(), [{"kind": "missing_merge_collection"}]
    active = []
    async for row in db[MERGES].find({"status": "active"}, {"_id": 0, "from_id": 1, "to_id": 1, "resolved_to_id": 1, "status": 1}):
        active.append(row)
    total = await db[MERGES].count_documents({})
    sources = [identity(row.get("from_id")) for row in active]
    source_counts = Counter(value for value in sources if value is not None)
    duplicates = {value: count for value, count in source_counts.items() if count > 1}
    retired_ids = set(source_counts)
    live = await active_ingredient_ids(db) if INGREDIENTS in exists else set()
    different_target = [identity(row.get("from_id")) for row in active if identity(row.get("to_id")) != identity(row.get("resolved_to_id"))]
    missing_resolved = [identity(row.get("from_id")) for row in active if identity(row.get("resolved_to_id")) not in live]
    retired_still_live = retired_ids.intersection(live)
    issues = []
    if duplicates:
        issues.append({"kind": "duplicate_active_from_id", "values": duplicates})
    if missing_resolved:
        issues.append({"kind": "resolved_target_not_active", "from_ids": sort_ids(set(missing_resolved))})
    if retired_still_live:
        issues.append({"kind": "retired_from_id_still_active", "from_ids": sort_ids(retired_still_live)})
    return {
        "collection_exists": True,
        "total_records": total,
        "active_records": len(active),
        "unique_active_from_id_count": len(retired_ids),
        "duplicate_active_from_ids": duplicates,
        "to_id_differs_from_resolved_to_id_count": len(different_target),
        "to_id_differs_from_resolved_to_id_from_ids": sort_ids(set(different_target)),
        "resolved_to_id_missing_active_ingredient_count": len(missing_resolved),
        "resolved_to_id_missing_active_ingredient_from_ids": sort_ids(set(missing_resolved)),
        "retired_from_id_still_active_ingredient_count": len(retired_still_live),
        "retired_from_id_still_active_ingredient_ids": sort_ids(retired_still_live),
    }, retired_ids, issues


async def scan_collection(db, collection, retired_ids):
    """Scan a collection without reporting source documents or PII."""
    paths = {}
    scanned = 0
    async for document in db[collection].find({}, {"_id": 0}):
        scanned += 1
        matches = retired_values_in_document(document, collection, retired_ids)
        for path, values in matches.items():
            result = paths.setdefault(path, {"affected_documents": 0, "retired_references": 0, "distinct_retired_ids": set()})
            result["affected_documents"] += 1
            result["retired_references"] += len(values)
            result["distinct_retired_ids"].update(values)
    rows = []
    for path, result in sorted(paths.items()):
        rows.append({"collection": collection, "field_path": path, "exists": True,
                     "documents_scanned": scanned, "affected_documents": result["affected_documents"],
                     "retired_references": result["retired_references"],
                     "distinct_retired_ids": sort_ids(result["distinct_retired_ids"]),
                     "migration_classification": classification(collection)})
    if not rows:
        rows.append({"collection": collection, "field_path": "<no retired ingredient reference found>", "exists": True,
                     "documents_scanned": scanned, "affected_documents": 0, "retired_references": 0,
                     "distinct_retired_ids": [], "migration_classification": classification(collection)})
    return rows


async def run(args):
    load_dotenv(ROOT / args.env_file)
    mongo_url, db_name = os.getenv("MONGO_URL"), os.getenv("DB_NAME")
    report = {
        "tool": "audit-retired-ingredient-ids-v3-2", "mode": "READ_ONLY",
        "started_at": now(), "source_environment_assumptions": {
            "required_database": EXPECTED_DB, "database_from_DB_NAME": db_name,
            "write_operations": "none", "pii_policy": "no source documents, document IDs, or user fields are reported",
        }, "exceptions": [], "collections_changed": [],
    }
    if not mongo_url or not db_name:
        raise RuntimeError("MONGO_URL and DB_NAME are required")
    if db_name != EXPECTED_DB:
        raise RuntimeError(f"Refusing database {db_name}; expected {EXPECTED_DB}")
    client = AsyncIOMotorClient(mongo_url, appname="audit-retired-ingredient-ids-v3-2")
    try:
        exists = set(await client[db_name].list_collection_names())
        report["collections_discovered"] = sorted(exists)
        integrity, retired_ids, integrity_issues = await merge_integrity(client[db_name], exists)
        report["merge_integrity"] = integrity
        report["exceptions"].extend(integrity_issues)
        if not retired_ids:
            report["audit_table"] = []
            report["migration_decision"] = "RUNTIME RESOLVER ONLY"
            report["acceptance_result"] = "FAIL" if integrity_issues else "PASS"
            return report

        table = []
        # ingredient_id_merges is assessed above, not reclassified as a consumer
        # of its own retired source IDs.  Ingredients gets a dedicated integrity row.
        for collection in sorted(exists - {MERGES, INGREDIENTS}):
            table.extend(await scan_collection(client[db_name], collection, retired_ids))
        ingredient_active_overlap = integrity.get("retired_from_id_still_active_ingredient_ids", [])
        table.append({"collection": INGREDIENTS, "field_path": "ingredient_id", "exists": INGREDIENTS in exists,
                      "documents_scanned": await client[db_name][INGREDIENTS].count_documents({}) if INGREDIENTS in exists else 0,
                      "affected_documents": len(ingredient_active_overlap), "retired_references": len(ingredient_active_overlap),
                      "distinct_retired_ids": ingredient_active_overlap,
                      "migration_classification": "canonical master/relationship data"})
        table.append({"collection": MERGES, "field_path": "from_id", "exists": MERGES in exists,
                      "documents_scanned": integrity.get("active_records", 0), "affected_documents": 0,
                      "retired_references": 0, "distinct_retired_ids": [],
                      "migration_classification": "canonical master/relationship data"})
        included = {row["collection"] for row in table}
        for collection in NAMED_COLLECTIONS:
            if collection not in included:
                table.append({"collection": collection, "field_path": "<collection absent>", "exists": False,
                              "documents_scanned": 0, "affected_documents": 0, "retired_references": 0,
                              "distinct_retired_ids": [], "migration_classification": classification(collection)})
        table.sort(key=lambda row: (row["collection"], row["field_path"]))
        report["audit_table"] = table

        affected = [row for row in table if row["affected_documents"]]
        operational = [row for row in affected if row["migration_classification"] != "historical/audit-only data"]
        report["summary"] = {
            "retired_ids_audited": sort_ids(retired_ids),
            "total_retired_ids": len(retired_ids),
            "total_retired_references": sum(row["retired_references"] for row in affected),
            "total_operational_retired_references": sum(row["retired_references"] for row in operational),
            "collections_requiring_migration": sorted({row["collection"] for row in operational}),
            "historical_or_audit_only_collections": sorted({row["collection"] for row in affected if row not in operational}),
        }
        report["migration_decision"] = "RUNTIME RESOLVER + DATA MIGRATION REQUIRED" if operational else "RUNTIME RESOLVER ONLY"
        report["acceptance_result"] = "PASS" if not integrity_issues else "FAIL"
        return report
    finally:
        client.close()
        report["finished_at"] = now()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--report", default="test_reports/retired-ingredient-id-audit-v3-2.json")
    args = parser.parse_args()
    report = {"tool": "audit-retired-ingredient-ids-v3-2", "mode": "READ_ONLY", "exceptions": []}
    try:
        report = asyncio.run(run(args))
    except Exception as error:
        report.setdefault("exceptions", []).append({"kind": "operation_error", "message": str(error)})
        report["acceptance_result"] = "FAIL"
        report["finished_at"] = now()
        raise
    finally:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps({"report": args.report, "acceptance_result": report.get("acceptance_result"),
                          "migration_decision": report.get("migration_decision"), "collections_changed": []}, indent=2))


if __name__ == "__main__":
    main()
