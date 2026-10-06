#!/usr/bin/env python3
"""Migrate v3.2 retired ingredient references to resolved canonical IDs.

Default operation is a read-only dry run. Writes require both ``--apply`` and
the confirmation token. The script never touches unrelated collections.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import DeleteOne, UpdateOne
from pymongo.errors import ConfigurationError, OperationFailure

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ingredient_resolution import IngredientResolutionError, parse_ingredient_id  # noqa: E402

EXPECTED_DB = "DrinkThinkv0"
CONFIRM = "MIGRATE_INGREDIENT_ID_MERGES_V3_2"
SCOPED_COLLECTIONS = ("ingredients", "cocktail_ingredients", "cocktails", "user_cupboard")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_default(value):
    return str(value)


def resolve_values(values, redirects):
    """Replace known retired values and deduplicate canonical results in order."""
    output, seen = [], set()
    for value in values or []:
        try:
            source_id = parse_ingredient_id(value)
        except IngredientResolutionError:
            # Preserve malformed values for manual remediation; never discard
            # data outside this migration's retired-ID scope.
            canonical_id = value
        else:
            canonical_id = redirects.get(source_id, source_id)
        marker = (type(canonical_id).__name__, str(canonical_id))
        if marker not in seen:
            seen.add(marker)
            output.append(canonical_id)
    return output


async def load_and_validate_mappings(database):
    rows = await database.ingredient_id_merges.find(
        {"status": "active"}, {"_id": 0, "from_id": 1, "to_id": 1, "resolved_to_id": 1, "status": 1}
    ).to_list(length=None)
    redirects = {}
    for row in rows:
        try:
            source_id = parse_ingredient_id(row.get("from_id"))
            resolved_id = parse_ingredient_id(row.get("resolved_to_id"))
        except IngredientResolutionError as error:
            raise RuntimeError(f"invalid active ingredient_id_merges row: {row}") from error
        if source_id == resolved_id:
            raise RuntimeError(f"merge source {source_id} resolves to itself")
        if source_id in redirects and redirects[source_id] != resolved_id:
            raise RuntimeError(f"conflicting active merge for {source_id}")
        redirects[source_id] = resolved_id
    if not redirects:
        raise RuntimeError("no active ingredient_id_merges records found")

    targets = sorted(set(redirects.values()))
    active_targets = {
        row["ingredient_id"]
        for row in await database.ingredients.find(
            {"ingredient_id": {"$in": targets}, "status": "active"},
            {"_id": 0, "ingredient_id": 1},
        ).to_list(length=None)
    }
    missing = sorted(set(targets) - active_targets)
    if missing:
        raise RuntimeError("active merge target(s) missing from active ingredients: " + ", ".join(map(str, missing)))
    return redirects, rows


async def mapping_comparison(database, redirects):
    ids = sorted(set(redirects) | set(redirects.values()))
    rows = await database.ingredients.find(
        {"ingredient_id": {"$in": ids}},
        {"_id": 0, "ingredient_id": 1, "name": 1, "status": 1, "ingredient_type": 1, "category_id": 1},
    ).to_list(length=None)
    by_id = {row.get("ingredient_id"): row for row in rows}
    return [
        {"from_id": source_id, "resolved_to_id": target_id,
         "source": by_id.get(source_id), "target": by_id.get(target_id)}
        for source_id, target_id in sorted(redirects.items())
    ]


async def reference_counts(database, source_ids):
    # The exact occurrence counts are calculated by aggregation so an array
    # containing two retired values is visible as two references.
    def count_from_result(rows):
        return rows[0]["count"] if rows else 0

    async def occurrences(collection, field):
        rows = await database[collection].aggregate([
            {"$unwind": f"${field}"},
            {"$match": {field: {"$in": source_ids}}},
            {"$count": "count"},
        ]).to_list(length=1)
        return count_from_result(rows)

    return {
        "active_retired_ingredients": await database.ingredients.count_documents(
            {"ingredient_id": {"$in": source_ids}, "status": "active"}
        ),
        "cocktail_ingredients_documents": await database.cocktail_ingredients.count_documents(
            {"ingredient_id": {"$in": source_ids}}
        ),
        "cocktail_ingredients_references": await database.cocktail_ingredients.count_documents(
            {"ingredient_id": {"$in": source_ids}}
        ),
        "cocktails_documents": await database.cocktails.count_documents(
            {"main_ingredient_ids": {"$in": source_ids}}
        ),
        "cocktails_references": await occurrences("cocktails", "main_ingredient_ids"),
        "user_cupboard_documents": await database.user_cupboard.count_documents(
            {"item_ids": {"$in": source_ids}}
        ),
        "user_cupboard_references": await occurrences("user_cupboard", "item_ids"),
    }


async def cocktail_ingredient_plan(database, redirects):
    source_ids, targets = list(redirects), list(set(redirects.values()))
    retired_rows = await database.cocktail_ingredients.find(
        {"ingredient_id": {"$in": source_ids}}, {"_id": 1, "cocktail_id": 1, "ingredient_id": 1, "role": 1, "sequence": 1}
    ).to_list(length=None)
    cocktail_ids = list({row.get("cocktail_id") for row in retired_rows})
    candidate_rows = await database.cocktail_ingredients.find(
        {"cocktail_id": {"$in": cocktail_ids}, "ingredient_id": {"$in": source_ids + targets}},
        {"_id": 1, "cocktail_id": 1, "ingredient_id": 1, "role": 1, "sequence": 1},
    ).to_list(length=None) if cocktail_ids else []

    def key(row, ingredient_id):
        return (row.get("cocktail_id"), ingredient_id, row.get("role"), row.get("sequence"))

    keepers, updates, deletes, collisions = {}, [], [], []
    for row in sorted(retired_rows, key=lambda item: str(item.get("_id"))):
        target_id = redirects[row["ingredient_id"]]
        relationship_key = key(row, target_id)
        existing = [
            candidate for candidate in candidate_rows
            if candidate.get("ingredient_id") == target_id
            and key(candidate, target_id) == relationship_key
            and candidate.get("_id") != row.get("_id")
        ]
        if existing:
            deletes.append(row["_id"])
            collisions.append({"cocktail_id": row.get("cocktail_id"), "from_id": row.get("ingredient_id"),
                               "resolved_to_id": target_id, "role": row.get("role"), "sequence": row.get("sequence"),
                               "action": "delete_retired_duplicate", "kept_ids": [str(item.get("_id")) for item in existing]})
        elif relationship_key in keepers:
            deletes.append(row["_id"])
            collisions.append({"cocktail_id": row.get("cocktail_id"), "from_id": row.get("ingredient_id"),
                               "resolved_to_id": target_id, "role": row.get("role"), "sequence": row.get("sequence"),
                               "action": "delete_converged_retired_duplicate", "kept_id": str(keepers[relationship_key])})
        else:
            keepers[relationship_key] = row["_id"]
            updates.append((row["_id"], row["ingredient_id"], target_id))
    return {"updates": updates, "deletes": deletes, "collisions": collisions}


async def array_plans(database, collection, field, redirects):
    source_ids = list(redirects)
    docs = await database[collection].find(
        {field: {"$in": source_ids}}, {"_id": 1, field: 1}
    ).to_list(length=None)
    return [
        (doc["_id"], doc.get(field) or [], resolve_values(doc.get(field), redirects))
        for doc in docs
    ]


async def apply_phase(database, name, operations, session=None):
    if not operations:
        return {"phase": name, "modified": 0, "deleted": 0}
    bulk = await database[name].bulk_write(operations, ordered=True, session=session)
    return {"phase": name, "modified": bulk.modified_count, "deleted": bulk.deleted_count}


async def apply_changes(database, redirects, plan, session=None):
    phases = []
    ci_operations = [DeleteOne({"_id": row_id}) for row_id in plan["cocktail_ingredients"]["deletes"]]
    ci_operations += [
        UpdateOne({"_id": row_id, "ingredient_id": source_id}, {"$set": {"ingredient_id": target_id}})
        for row_id, source_id, target_id in plan["cocktail_ingredients"]["updates"]
    ]
    phases.append(await apply_phase(database, "cocktail_ingredients", ci_operations, session=session))

    for collection, field in (("cocktails", "main_ingredient_ids"), ("user_cupboard", "item_ids")):
        operations = [
            UpdateOne({"_id": row_id}, {"$set": {field: values}})
            for row_id, _before, values in plan[collection]
        ]
        phases.append(await apply_phase(database, collection, operations, session=session))

    retired_at = now()
    ingredient_operations = [
        UpdateOne({"ingredient_id": source_id, "status": "active"}, {"$set": {
            "status": "inactive", "updated_at": retired_at,
        }})
        for source_id in redirects
    ]
    phases.append(await apply_phase(database, "ingredients", ingredient_operations, session=session))
    return phases


async def run(args):
    load_dotenv(ROOT / args.env_file)
    mongo_url, db_name = os.getenv("MONGO_URL"), os.getenv("DB_NAME")
    if not mongo_url or not db_name:
        raise RuntimeError("MONGO_URL and DB_NAME are required")
    if db_name != EXPECTED_DB:
        raise RuntimeError(f"Refusing database {db_name}; expected {EXPECTED_DB}")

    client = AsyncIOMotorClient(mongo_url, appname="migrate-ingredient-id-merges-v3-2")
    database = client[db_name]
    try:
        redirects, merge_rows = await load_and_validate_mappings(database)
        source_ids = sorted(redirects)
        before = await reference_counts(database, source_ids)
        plan = {
            "cocktail_ingredients": await cocktail_ingredient_plan(database, redirects),
            "cocktails": await array_plans(database, "cocktails", "main_ingredient_ids", redirects),
            "user_cupboard": await array_plans(database, "user_cupboard", "item_ids", redirects),
        }
        report = {
            "tool": "migrate-ingredient-id-merges-v3-2", "mode": "APPLY" if args.apply else "DRY_RUN",
            "database": db_name, "scoped_collections": list(SCOPED_COLLECTIONS), "started_at": now(),
            "active_mapping_count": len(merge_rows), "active_mappings": merge_rows,
            "mapping_comparison": await mapping_comparison(database, redirects),
            "before_counts": before,
            "planned": {
                "cocktail_ingredients_updates": len(plan["cocktail_ingredients"]["updates"]),
                "cocktail_ingredients_deletes": len(plan["cocktail_ingredients"]["deletes"]),
                "cocktails_updates": len(plan["cocktails"]), "user_cupboard_updates": len(plan["user_cupboard"]),
                "active_ingredients_to_retire": before["active_retired_ingredients"],
                "merge_sources_checked": len(redirects), "collisions": plan["cocktail_ingredients"]["collisions"],
            },
        }
        if not args.apply:
            report["after_counts"] = "NOT_APPLICABLE_DRY_RUN"
            report["acceptance_result"] = "READY_FOR_APPLY"
            return report

        if args.transaction_mode == "off":
            report["transaction"] = "non_transactional_resumable_phases"
            report["phases"] = await apply_changes(database, redirects, plan)
        else:
            try:
                async with await client.start_session() as session:
                    async with session.start_transaction():
                        report["phases"] = await apply_changes(database, redirects, plan, session=session)
                report["transaction"] = "committed"
            except (ConfigurationError, OperationFailure) as error:
                unsupported = (
                    isinstance(error, ConfigurationError)
                    or getattr(error, "code", None) in {20, 303}
                    or "Transaction numbers are only allowed" in str(error)
                )
                if args.transaction_mode == "required" or not unsupported:
                    if not unsupported:
                        raise
                    raise RuntimeError("transaction unavailable; rerun only after assessing --transaction-mode off") from error
                report["transaction"] = "unavailable; applied resumable phases"
                report["transaction_error"] = str(error)
                report["phases"] = await apply_changes(database, redirects, plan)
        after = await reference_counts(database, source_ids)
        report["after_counts"] = after
        if any(after.values()):
            raise RuntimeError("post-migration validation found active retired references: " + json.dumps(after))
        report["acceptance_result"] = "PASS"
        return report
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--report", default="test_reports/ingredient-id-merges-v3-2.json")
    parser.add_argument("--dry-run", action="store_true", help="Explicit read-only mode (the default)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-apply", default="")
    parser.add_argument("--transaction-mode", choices=("auto", "required", "off"), default="auto")
    args = parser.parse_args()
    if args.apply and args.dry_run:
        raise SystemExit("--dry-run and --apply cannot be used together")
    if args.apply and args.confirm_apply != CONFIRM:
        raise SystemExit(f"--apply requires --confirm-apply {CONFIRM}")
    report = {"tool": "migrate-ingredient-id-merges-v3-2", "acceptance_result": "FAIL"}
    try:
        report = asyncio.run(run(args))
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        report["finished_at"] = now()
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, default=json_default) + "\n", encoding="utf-8")
        print(json.dumps({"report": str(path), "acceptance_result": report.get("acceptance_result")}, indent=2))


if __name__ == "__main__":
    main()
