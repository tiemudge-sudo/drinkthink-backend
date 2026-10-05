#!/usr/bin/env python3
"""Safely audit or apply the canonical DrinkThink schema-v3.2 package.

The default invocation is read-only.  Applying requires both --apply and the
exact confirmation token below.  This tool never reads from or writes to the
legacy ``drinks`` collection and never deletes documents.
"""
import argparse
import asyncio
import json
import os
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_DB = "DrinkThinkv0"
CONFIRM = "APPLY_CANONICAL_SCHEMA_V3_2"
PACKAGE_COUNTS = {
    "cocktails": 16921,
    "glass_categories": 10,
    "glasses": 45,
    "legacy_glasses": 62,
    "ingredient_id_merges": 153,
}
PACKAGE_FILES = {
    "cocktails": "cocktails.json",
    "glass_categories": "glass_categories.json",
    "glasses": "glasses.json",
    "legacy_glasses": "legacy_glasses.json",
    "ingredient_id_merges": "ingredient_id_merge_map.json",
}
COCKTAIL_GLASS_FIELDS = (
    "glass_id", "glass_category_id", "glass_category", "prior_glass_id",
    "legacy_glass_id", "legacy_glass",
)
INDEX_REQUIREMENTS = (
    ("glass_categories", "glass_category_id"),
    ("glasses", "glass_id"),
    ("legacy_glasses", "legacy_glass_id"),
    ("ingredient_id_merges", "from_id"),
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def json_default(value):
    return str(value)


def normalized_id(value):
    """Compare numeric/string BSON identity values without name matching."""
    if value is None:
        return None
    return str(value)


def sample(values, limit=25):
    return list(values)[:limit]


def load_package(package_path, merge_entries_path):
    """Load the canonical ZIP or its identical unpacked directory form."""
    required = {"MANIFEST.json", *PACKAGE_FILES.values()}
    if package_path.is_dir():
        missing = sorted(name for name in required if not (package_path / name).is_file())
        if missing:
            raise RuntimeError("source directory missing required files: " + ", ".join(missing))

        def read_json(name):
            return json.loads((package_path / name).read_text(encoding="utf-8"))
    elif package_path.is_file():
        archive = zipfile.ZipFile(package_path)
        try:
            names = set(archive.namelist())
            missing = sorted(required - names)
            if missing:
                raise RuntimeError("package missing required files: " + ", ".join(missing))

            def read_json(name):
                with archive.open(name) as source:
                    return json.load(source)

            manifest = read_json("MANIFEST.json")
            data = {key: read_json(filename) for key, filename in PACKAGE_FILES.items()}
        finally:
            archive.close()
    else:
        raise RuntimeError("source package path does not exist")

    if package_path.is_dir():
        manifest = read_json("MANIFEST.json")
        data = {key: read_json(filename) for key, filename in PACKAGE_FILES.items()}

    merge_artifact = data["ingredient_id_merges"]
    if not isinstance(merge_artifact, dict) or not isinstance(merge_artifact.get("map"), dict):
        raise RuntimeError("ingredient_id_merge_map.json is not the expected map artifact")
    if not merge_entries_path.is_file():
        raise RuntimeError("ingredient_id_merge_map_entries.json does not exist")
    data["ingredient_id_merges"] = json.loads(merge_entries_path.read_text(encoding="utf-8"))
    if not isinstance(data["ingredient_id_merges"], list):
        raise RuntimeError("ingredient_id_merge_map_entries.json must contain an array")
    return manifest, data, merge_artifact


def package_validation(manifest, data, merge_artifact):
    exceptions = []
    counts = {name: len(rows) for name, rows in data.items()}
    for name, expected in PACKAGE_COUNTS.items():
        if counts[name] != expected:
            exceptions.append({"kind": "unexpected_package_count", "collection": name,
                               "actual": counts[name], "expected": expected})

    manifest_counts = {name: (manifest.get("collections", {}).get(name, {}).get("count"))
                       for name in ("cocktails", "glass_categories", "glasses", "legacy_glasses")}
    for name, actual in manifest_counts.items():
        if actual != counts[name]:
            exceptions.append({"kind": "manifest_count_mismatch", "collection": name,
                               "manifest": actual, "file": counts[name]})

    identities = {
        "cocktails": "cocktail_id",
        "glass_categories": "glass_category_id",
        "glasses": "glass_id",
        "legacy_glasses": "legacy_glass_id",
        "ingredient_id_merges": "from_id",
    }
    for collection, key in identities.items():
        values = [normalized_id(row.get(key)) for row in data[collection]]
        blanks = sum(value is None or value == "" for value in values)
        duplicates = [value for value, count in Counter(values).items() if value and count > 1]
        if blanks:
            exceptions.append({"kind": "missing_canonical_id", "collection": collection, "field": key, "count": blanks})
        if duplicates:
            exceptions.append({"kind": "duplicate_canonical_id", "collection": collection, "field": key,
                               "count": len(duplicates), "samples": sample(sorted(duplicates))})

    category_ids = {row["glass_category_id"] for row in data["glass_categories"]}
    glass_ids = {row["glass_id"] for row in data["glasses"]}
    for row in data["glasses"]:
        if row.get("glass_category_id") not in category_ids:
            exceptions.append({"kind": "glass_missing_category", "glass_id": row.get("glass_id"),
                               "glass_category_id": row.get("glass_category_id")})
    for row in data["legacy_glasses"]:
        if row.get("glass_id") not in glass_ids or row.get("glass_category_id") not in category_ids:
            exceptions.append({"kind": "legacy_glass_missing_parent", "legacy_glass_id": row.get("legacy_glass_id"),
                               "glass_id": row.get("glass_id"), "glass_category_id": row.get("glass_category_id")})
    legacy_ids = {row["legacy_glass_id"] for row in data["legacy_glasses"]}
    for row in data["cocktails"]:
        if row.get("glass_id") not in glass_ids or row.get("glass_category_id") not in category_ids:
            exceptions.append({"kind": "cocktail_missing_glass_parent", "cocktail_id": row.get("cocktail_id"),
                               "glass_id": row.get("glass_id"), "glass_category_id": row.get("glass_category_id")})
        if row.get("legacy_glass_id") and row["legacy_glass_id"] not in legacy_ids:
            exceptions.append({"kind": "cocktail_missing_legacy_glass_parent", "cocktail_id": row.get("cocktail_id"),
                               "legacy_glass_id": row.get("legacy_glass_id")})

    merge_map = {row["from_id"]: row["to_id"] for row in data["ingredient_id_merges"]}
    # Entries are materialized from the package's map and resolved_map.  Verify
    # every required operational field before a database connection is used.
    for row in data["ingredient_id_merges"]:
        if any(row.get(field) is None for field in ("from_id", "to_id", "resolved_to_id")) or row.get("status") != "active":
            exceptions.append({"kind": "invalid_ingredient_merge_entry", "entry": row})
    for source in merge_map:
        seen, cursor = set(), source
        while cursor in merge_map:
            if cursor in seen:
                exceptions.append({"kind": "ingredient_merge_cycle", "from_id": source,
                                   "cycle_at": cursor})
                break
            seen.add(cursor)
            cursor = merge_map[cursor]
        expected_resolved = cursor
        entry = next(row for row in data["ingredient_id_merges"] if row["from_id"] == source)
        if entry["resolved_to_id"] != expected_resolved:
            exceptions.append({"kind": "ingredient_resolved_target_mismatch", "from_id": source,
                               "expected": expected_resolved, "actual": entry["resolved_to_id"]})
        source_key = str(source)
        if merge_artifact["map"].get(source_key) != entry["to_id"] or merge_artifact.get("resolved_map", {}).get(source_key) != entry["resolved_to_id"]:
            exceptions.append({"kind": "ingredient_entry_export_mismatch", "from_id": source})
    return counts, exceptions


async def collection_id_state(collection, field):
    values = []
    async for doc in collection.find({}, {field: 1}):
        values.append(normalized_id(doc.get(field)))
    return values


async def audit_database(db, data):
    """Read all relevant identities and relationships.  No mutation occurs here."""
    report, exceptions = {"collection_counts": {}}, []
    involved = ["cocktails", "cocktail_ingredients", "ingredients", "glass_categories",
                "glasses", "legacy_glasses", "ingredient_id_merges"]
    for name in involved:
        report["collection_counts"][name] = await db[name].count_documents({})

    ids = {}
    for collection, field in (("cocktails", "cocktail_id"), ("glass_categories", "glass_category_id"),
                              ("glasses", "glass_id"), ("legacy_glasses", "legacy_glass_id"),
                              ("ingredient_id_merges", "from_id")):
        values = await collection_id_state(db[collection], field)
        duplicates = [value for value, count in Counter(values).items() if value and count > 1]
        blanks = sum(value is None or value == "" for value in values)
        report[f"{collection}_identity"] = {"count": len(values), "blank_count": blanks,
                                               "duplicate_count": len(duplicates), "duplicate_samples": sample(duplicates)}
        if blanks or duplicates:
            exceptions.append({"kind": "database_identity_not_unique", "collection": collection,
                               "blank_count": blanks, "duplicate_samples": sample(duplicates)})
        ids[collection] = set(values)

    package_ids = {
        "cocktails": {normalized_id(row["cocktail_id"]) for row in data["cocktails"]},
        "glass_categories": {normalized_id(row["glass_category_id"]) for row in data["glass_categories"]},
        "glasses": {normalized_id(row["glass_id"]) for row in data["glasses"]},
        "legacy_glasses": {normalized_id(row["legacy_glass_id"]) for row in data["legacy_glasses"]},
        "ingredient_id_merges": {normalized_id(row["from_id"]) for row in data["ingredient_id_merges"]},
    }
    report["package_vs_database"] = {}
    for collection in package_ids:
        missing = sorted(package_ids[collection] - ids[collection])
        extra = sorted(ids[collection] - package_ids[collection])
        report["package_vs_database"][collection] = {"package_count": len(package_ids[collection]),
                                                       "database_count": len(ids[collection]),
                                                       "missing_from_database_count": len(missing),
                                                       "extra_in_database_count": len(extra),
                                                       "missing_samples": sample(missing), "extra_samples": sample(extra)}
        # Cocktails must reconcile before any mutation.  The schema collections must
        # reconcile too, because this tool intentionally has no delete behaviour.
        if collection == "cocktails" and (missing or extra):
            exceptions.append({"kind": "canonical_cocktail_ids_do_not_reconcile", "missing_count": len(missing), "extra_count": len(extra)})
        if collection != "cocktails" and extra:
            exceptions.append({"kind": "unexpected_database_records_would_prevent_expected_count", "collection": collection, "extra_count": len(extra), "samples": sample(extra)})

    cocktail_changes = []
    package_by_cid = {row["cocktail_id"]: row for row in data["cocktails"]}
    async for doc in db.cocktails.find({}, {"_id": 0, "cocktail_id": 1, **{field: 1 for field in COCKTAIL_GLASS_FIELDS}}):
        source = package_by_cid.get(doc.get("cocktail_id"))
        if not source:
            continue
        before = {field: doc.get(field) for field in COCKTAIL_GLASS_FIELDS}
        after = {field: source.get(field) for field in COCKTAIL_GLASS_FIELDS}
        if before != after:
            cocktail_changes.append({"cocktail_id": doc["cocktail_id"], "before": before, "after": after})
    report["proposed_cocktail_glass_updates"] = {"count": len(cocktail_changes), "samples": sample(cocktail_changes)}

    ingredient_ids = set()
    async for ingredient in db.ingredients.find({}, {"ingredient_id": 1}):
        identity = normalized_id(ingredient.get("ingredient_id"))
        if identity:
            ingredient_ids.add(identity)
    missing_cocktail_refs, missing_ingredient_refs = [], []
    async for row in db.cocktail_ingredients.find({}, {"cocktail_id": 1, "ingredient_id": 1}):
        if normalized_id(row.get("cocktail_id")) not in ids["cocktails"]:
            missing_cocktail_refs.append({"cocktail_id": row.get("cocktail_id"), "ingredient_id": row.get("ingredient_id")})
        if normalized_id(row.get("ingredient_id")) not in ingredient_ids:
            missing_ingredient_refs.append({"cocktail_id": row.get("cocktail_id"), "ingredient_id": row.get("ingredient_id")})
    report["relationship_validation"] = {
        "cocktail_ingredients_missing_cocktail_count": len(missing_cocktail_refs),
        "cocktail_ingredients_missing_cocktail_samples": sample(missing_cocktail_refs),
        "cocktail_ingredients_missing_ingredient_count": len(missing_ingredient_refs),
        "cocktail_ingredients_missing_ingredient_samples": sample(missing_ingredient_refs),
    }
    if missing_cocktail_refs or missing_ingredient_refs:
        exceptions.append({"kind": "cocktail_ingredient_referential_failure",
                           "missing_cocktail_count": len(missing_cocktail_refs),
                           "missing_ingredient_count": len(missing_ingredient_refs)})

    missing_merge_targets = [row for row in data["ingredient_id_merges"]
                             if normalized_id(row["resolved_to_id"]) not in ingredient_ids]
    report["ingredient_merge_target_validation"] = {"live_ingredient_count": len(ingredient_ids),
        "missing_live_target_count": len(missing_merge_targets), "samples": sample(missing_merge_targets)}
    if missing_merge_targets:
        exceptions.append({"kind": "ingredient_merge_target_not_live", "count": len(missing_merge_targets),
                           "samples": sample(missing_merge_targets)})
    return report, exceptions


async def build_operations(db, data):
    """Compute inserts/updates unchanged; this method does not mutate the database."""
    plan = {}
    specs = (("glass_categories", "glass_category_id", data["glass_categories"]),
             ("glasses", "glass_id", data["glasses"]),
             ("legacy_glasses", "legacy_glass_id", data["legacy_glasses"]),
             ("ingredient_id_merges", "from_id", data["ingredient_id_merges"]))
    for collection, key, rows in specs:
        inserts = updates = unchanged = 0
        details = []
        for row in rows:
            old = await db[collection].find_one({key: row[key]}, {"_id": 0})
            if old is None:
                inserts += 1
            elif all(old.get(field) == value for field, value in row.items()):
                unchanged += 1
            else:
                updates += 1
                if len(details) < 25:
                    details.append({"identity": row[key], "changed_fields": [field for field, value in row.items() if old.get(field) != value]})
        plan[collection] = {"insert_count": inserts, "update_count": updates, "unchanged_count": unchanged,
                            "delete_count": 0, "update_samples": details}
    # Cocktails are intentionally limited to the glass hierarchy fields.
    inserts = updates = unchanged = 0
    # One bounded collection scan avoids 16,921 sequential network round trips.
    # The package/database identity reconciliation has already been validated.
    current_cocktails = {}
    projection = {"_id": 0, "cocktail_id": 1, **{field: 1 for field in COCKTAIL_GLASS_FIELDS}}
    async for doc in db.cocktails.find({}, projection):
        current_cocktails[doc.get("cocktail_id")] = doc
    for row in data["cocktails"]:
        old = current_cocktails.get(row["cocktail_id"])
        if old is None:
            inserts += 1
        elif any(old.get(field) != row.get(field) for field in COCKTAIL_GLASS_FIELDS):
            updates += 1
        else:
            unchanged += 1
    plan["cocktails"] = {"insert_count": inserts, "update_count": updates, "unchanged_count": unchanged,
                         "delete_count": 0, "allowed_fields": list(COCKTAIL_GLASS_FIELDS)}
    return plan


async def audit_index_readiness(db):
    """Accept an equivalent unique index even when it has a legacy name."""
    statuses, exceptions = [], []
    for collection, field in INDEX_REQUIREMENTS:
        indexes = await db[collection].index_information()
        matching = []
        for name, info in indexes.items():
            key = list((info.get("key") or {}).items())
            if key == [(field, 1)]:
                matching.append({"name": name, "unique": bool(info.get("unique"))})
        unique_names = [item["name"] for item in matching if item["unique"]]
        incompatible_names = [item["name"] for item in matching if not item["unique"]]
        status = {"collection": collection, "field": field, "existing_indexes": matching,
                  "satisfied_by_existing_unique_index": bool(unique_names),
                  "will_create": not matching}
        statuses.append(status)
        if incompatible_names:
            exceptions.append({"kind": "incompatible_non_unique_index", "collection": collection,
                               "field": field, "indexes": incompatible_names,
                               "required": "unique index; no automatic index removal is permitted"})
        elif len(unique_names) > 1:
            exceptions.append({"kind": "duplicate_equivalent_unique_indexes", "collection": collection,
                               "field": field, "indexes": unique_names})
    return statuses, exceptions


async def apply_changes(db, data):
    changed = []
    for collection, key, rows in (("glass_categories", "glass_category_id", data["glass_categories"]),
                                  ("glasses", "glass_id", data["glasses"]),
                                  ("legacy_glasses", "legacy_glass_id", data["legacy_glasses"]),
                                  ("ingredient_id_merges", "from_id", data["ingredient_id_merges"])):
        modified = upserted = 0
        for row in rows:
            result = await db[collection].update_one({key: row[key]}, {"$set": row}, upsert=True)
            modified += result.modified_count
            upserted += int(result.upserted_id is not None)
        changed.append({"collection": collection, "modified_count": modified, "upserted_count": upserted})

    current_cocktails = {}
    projection = {"_id": 0, "cocktail_id": 1, **{field: 1 for field in COCKTAIL_GLASS_FIELDS}}
    async for doc in db.cocktails.find({}, projection):
        current_cocktails[doc.get("cocktail_id")] = doc
    operations = []
    for row in data["cocktails"]:
        fields = {field: row.get(field) for field in COCKTAIL_GLASS_FIELDS}
        current = current_cocktails.get(row["cocktail_id"])
        if current is None:
            raise RuntimeError("cocktail identity disappeared before apply: " + row["cocktail_id"])
        if any(current.get(field) != value for field, value in fields.items()):
            operations.append(UpdateOne({"cocktail_id": row["cocktail_id"]}, {"$set": fields}))
    modified = matched = 0
    for start in range(0, len(operations), 500):
        result = await db.cocktails.bulk_write(operations[start:start + 500], ordered=True)
        modified += result.modified_count
        matched += result.matched_count
    if matched != len(operations):
        raise RuntimeError("one or more cocktail identities disappeared during apply")
    changed.append({"collection": "cocktails", "modified_count": modified, "upserted_count": 0,
                    "fields_limited_to": list(COCKTAIL_GLASS_FIELDS)})

    readiness, index_exceptions = await audit_index_readiness(db)
    if index_exceptions:
        raise RuntimeError("APPLY BLOCKED: index readiness changed after preflight")
    indexes = []
    for status in readiness:
        collection, field = status["collection"], status["field"]
        if status["satisfied_by_existing_unique_index"]:
            indexes.append({"collection": collection, "index": status["existing_indexes"][0]["name"],
                            "created": False, "unique": True, "key": field})
            continue
        name = f"uniq_{field}"
        await db[collection].create_index([(field, 1)], unique=True, name=name)
        indexes.append({"collection": collection, "index": name, "created": True,
                        "unique": True, "key": field})
    return changed, indexes


async def validate_post_state(db, data):
    """Validate actual persisted documents, not merely the source package."""
    exceptions = []
    comparisons = {}
    for collection, key, rows, fields in (
        ("glass_categories", "glass_category_id", data["glass_categories"], None),
        ("glasses", "glass_id", data["glasses"], None),
        ("legacy_glasses", "legacy_glass_id", data["legacy_glasses"], None),
        ("ingredient_id_merges", "from_id", data["ingredient_id_merges"], None),
        ("cocktails", "cocktail_id", data["cocktails"], COCKTAIL_GLASS_FIELDS),
    ):
        actual_by_identity = {}
        if collection == "cocktails":
            projection = {"_id": 0, "cocktail_id": 1, **{field: 1 for field in COCKTAIL_GLASS_FIELDS}}
            async for doc in db.cocktails.find({}, projection):
                actual_by_identity[doc.get("cocktail_id")] = doc
        mismatches = []
        for expected in rows:
            actual = actual_by_identity.get(expected[key]) if collection == "cocktails" else await db[collection].find_one({key: expected[key]}, {"_id": 0})
            compared_fields = fields or tuple(expected.keys())
            changed_fields = [field for field in compared_fields if (actual or {}).get(field) != expected.get(field)]
            if changed_fields:
                mismatches.append({"identity": expected[key], "changed_fields": changed_fields})
        comparisons[collection] = {"mismatch_count": len(mismatches), "samples": sample(mismatches)}
        if mismatches:
            exceptions.append({"kind": "package_database_field_mismatch", "collection": collection,
                               "count": len(mismatches), "samples": sample(mismatches)})

    category_ids = set(await collection_id_state(db.glass_categories, "glass_category_id"))
    glass_ids = set(await collection_id_state(db.glasses, "glass_id"))
    legacy_ids = set(await collection_id_state(db.legacy_glasses, "legacy_glass_id"))
    missing = {"glass_category_parent": [], "legacy_glass_parent": [], "cocktail_glass_parent": [],
               "cocktail_category_parent": [], "cocktail_legacy_parent": []}
    async for row in db.glasses.find({}, {"glass_id": 1, "glass_category_id": 1}):
        if normalized_id(row.get("glass_category_id")) not in category_ids:
            missing["glass_category_parent"].append({"glass_id": row.get("glass_id"), "glass_category_id": row.get("glass_category_id")})
    async for row in db.legacy_glasses.find({}, {"legacy_glass_id": 1, "glass_id": 1, "glass_category_id": 1}):
        if normalized_id(row.get("glass_id")) not in glass_ids or normalized_id(row.get("glass_category_id")) not in category_ids:
            missing["legacy_glass_parent"].append({"legacy_glass_id": row.get("legacy_glass_id")})
    async for row in db.cocktails.find({}, {"cocktail_id": 1, "glass_id": 1, "glass_category_id": 1, "legacy_glass_id": 1}):
        cid = row.get("cocktail_id")
        if normalized_id(row.get("glass_id")) not in glass_ids:
            missing["cocktail_glass_parent"].append(cid)
        if normalized_id(row.get("glass_category_id")) not in category_ids:
            missing["cocktail_category_parent"].append(cid)
        if row.get("legacy_glass_id") and normalized_id(row.get("legacy_glass_id")) not in legacy_ids:
            missing["cocktail_legacy_parent"].append(cid)
    relationship_result = {key: {"count": len(rows), "samples": sample(rows)} for key, rows in missing.items()}
    if any(missing.values()):
        exceptions.append({"kind": "persisted_glass_relationship_failure", "details": relationship_result})
    return {"package_vs_database": comparisons, "referential_integrity": relationship_result}, exceptions


async def run(args):
    package = Path(args.package)
    entries = Path(args.merge_entries)
    manifest, data, merge_export = load_package(package, entries)
    package_counts, package_exceptions = package_validation(manifest, data, merge_export)
    report = {
        "tool": "apply-canonical-schema-v3.2", "mode": "APPLY" if args.apply else "DRY_RUN",
        "started_at": utc_now(), "source_environment_assumptions": {"required_database": EXPECTED_DB,
        "database_from_DB_NAME": None, "source_package": str(package), "merge_entries_source": str(entries), "source_manifest": manifest,
        "prohibited_collections": ["drinks"]}, "package_counts": package_counts,
        "package_validation": {"passed": not package_exceptions, "exceptions": package_exceptions},
        "collections_changed": [], "exceptions": list(package_exceptions),
    }
    load_dotenv(ROOT / args.env_file)
    mongo_url, db_name = os.getenv("MONGO_URL"), os.getenv("DB_NAME")
    report["source_environment_assumptions"]["database_from_DB_NAME"] = db_name
    if not mongo_url or not db_name:
        raise RuntimeError("MONGO_URL and DB_NAME are required")
    if db_name != EXPECTED_DB:
        report["exceptions"].append({"kind": "unexpected_database", "actual": db_name, "expected": EXPECTED_DB})
        raise RuntimeError(f"Refusing database {db_name}; expected {EXPECTED_DB}")

    client = AsyncIOMotorClient(mongo_url, appname="apply-canonical-schema-v3-2")
    try:
        database_audit, database_exceptions = await audit_database(client[db_name], data)
        report["preflight_audit"] = database_audit
        report["exceptions"].extend(database_exceptions)
        report["proposed_operations"] = await build_operations(client[db_name], data)
        report["index_preflight"], index_exceptions = await audit_index_readiness(client[db_name])
        report["exceptions"].extend(index_exceptions)
        gates = {"package_validation_passed": not package_exceptions,
                 "database_validation_passed": not database_exceptions,
                 "index_readiness_passed": not index_exceptions,
                 "no_deletes_are_planned": all(item["delete_count"] == 0 for item in report["proposed_operations"].values()),
                 "cocktails_have_no_insert_plan": report["proposed_operations"]["cocktails"]["insert_count"] == 0}
        report["pre_write_validation"] = gates
        report["pre_write_acceptance_result"] = "PASS" if all(gates.values()) else "FAIL"
        if args.apply:
            if not all(gates.values()):
                raise RuntimeError("APPLY BLOCKED: pre-write acceptance gate failed")
            report["collections_changed"], report["indexes"] = await apply_changes(client[db_name], data)
            post_audit, post_exceptions = await audit_database(client[db_name], data)
            report["post_write_audit"] = post_audit
            package_comparison, package_comparison_exceptions = await validate_post_state(client[db_name], data)
            all_post_exceptions = post_exceptions + package_comparison_exceptions
            report["post_write_validation"] = {"passed": not all_post_exceptions, "exceptions": all_post_exceptions,
                "package_vs_database": package_comparison,
                "expected_collection_counts": {name: post_audit["collection_counts"].get(name) == expected
                    for name, expected in PACKAGE_COUNTS.items()}}
            expected_counts_ok = all(report["post_write_validation"]["expected_collection_counts"].values())
            report["acceptance_result"] = "PASS" if not all_post_exceptions and expected_counts_ok else "FAIL"
        else:
            report["post_write_validation"] = "NOT_APPLICABLE_DRY_RUN"
            report["acceptance_result"] = "READY_FOR_APPLY" if all(gates.values()) else "FAIL"
    finally:
        client.close()
        report["finished_at"] = utc_now()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, help="Path to atlas_push_glass (1).zip or its unpacked source directory")
    parser.add_argument("--merge-entries", required=True, help="Path to ingredient_id_merge_map_entries.json")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--report", default="test_reports/canonical-schema-v3-2-audit.json")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-apply", default="")
    args = parser.parse_args()
    if args.apply and args.confirm_apply != CONFIRM:
        raise SystemExit(f"--apply requires --confirm-apply {CONFIRM}")
    report = {"tool": "apply-canonical-schema-v3.2", "mode": "APPLY" if args.apply else "DRY_RUN", "exceptions": []}
    try:
        report = asyncio.run(run(args))
    except Exception as error:
        report.setdefault("exceptions", []).append({"kind": "operation_error", "message": str(error)})
        report["acceptance_result"] = "FAIL"
        report["finished_at"] = utc_now()
        raise
    finally:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, default=json_default) + "\n", encoding="utf-8")
        print(json.dumps({"report": args.report, "acceptance_result": report.get("acceptance_result"),
                          "collections_changed": report.get("collections_changed", [])}, indent=2, default=json_default))


if __name__ == "__main__":
    main()
