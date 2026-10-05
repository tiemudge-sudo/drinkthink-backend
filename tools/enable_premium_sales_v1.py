#!/usr/bin/env python3
"""Audit and controlled enablement of v1 Premium sales remote flags.

Default mode is read-only. Apply mode can only insert the missing canonical
DrinkThinkv0.app_config/flags document; it never updates another document.
"""
import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_DB = "DrinkThinkv0"
FLAGS_ID = "flags"
CHECK_IN_ID = "location_check_in"
CONFIRM = "ENABLE_PREMIUM_SALES_V1"
DESIRED_FLAGS = {
    "consumer_ordering_enabled": False,
    "consumer_premium_sales_enabled": True,
}


def now():
    return datetime.now(timezone.utc).isoformat()


def public_document(doc):
    """Retain config state for audit without serializing any unrelated BSON data."""
    if not doc:
        return None
    return {key: value for key, value in doc.items() if key in {
        "_id", "consumer_ordering_enabled", "consumer_premium_sales_enabled",
        "check_in_radius_feet", "created_at", "updated_at",
    }}


async def read_state(collection):
    docs = await collection.find({}, {"_id": 1, "consumer_ordering_enabled": 1,
                                      "consumer_premium_sales_enabled": 1,
                                      "check_in_radius_feet": 1, "created_at": 1,
                                      "updated_at": 1}).to_list(length=None)
    flags = next((doc for doc in docs if doc.get("_id") == FLAGS_ID), None)
    check_in = next((doc for doc in docs if doc.get("_id") == CHECK_IN_ID), None)
    return docs, flags, check_in


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--report", default="premium-sales-v1-audit.json")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-apply", default="")
    args = parser.parse_args()
    if args.apply and args.confirm_apply != CONFIRM:
        raise SystemExit(f"--apply requires --confirm-apply {CONFIRM}")

    load_dotenv(ROOT / args.env_file)
    mongo_url, db_name = os.getenv("MONGO_URL"), os.getenv("DB_NAME")
    if not mongo_url or not db_name:
        raise SystemExit("MONGO_URL and DB_NAME are required")

    report = {
        "tool": "enable-premium-sales-v1",
        "mode": "APPLY" if args.apply else "DRY_RUN",
        "started_at": now(),
        "source_environment_assumptions": {
            "database_from_DB_NAME": db_name,
            "required_database": EXPECTED_DB,
            "collection": "app_config",
            "target_document_id": FLAGS_ID,
            "protected_document_id": CHECK_IN_ID,
        },
        "desired_flags": DESIRED_FLAGS,
        "collections_changed": [],
        "exceptions": [],
    }
    if db_name != EXPECTED_DB:
        report["exceptions"].append({"kind": "unexpected_database", "actual": db_name, "expected": EXPECTED_DB})
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        raise SystemExit(f"Refusing database {db_name}; expected {EXPECTED_DB}")

    client = AsyncIOMotorClient(mongo_url, appname="enable-premium-sales-v1")
    try:
        collection = client[db_name].app_config
        before_docs, before_flags, before_check_in = await read_state(collection)
        report["before_state"] = {
            "app_config_document_count": len(before_docs),
            "document_ids": sorted(str(doc.get("_id")) for doc in before_docs),
            "flags": public_document(before_flags),
            "location_check_in": public_document(before_check_in),
        }
        gates = {
            "target_database_is_exactly_DrinkThinkv0": db_name == EXPECTED_DB,
            "location_check_in_exists": before_check_in is not None,
            "flags_document_is_absent_before_write": before_flags is None,
        }
        report["pre_write_validation"] = gates
        report["pre_write_acceptance_result"] = "PASS" if all(gates.values()) else "FAIL"
        if args.apply:
            if not all(gates.values()):
                raise RuntimeError("APPLY BLOCKED: pre-write validation did not pass")
            # $setOnInsert means a concurrent/existing flags document is never
            # overwritten. No timestamps: the current /api/config flags writer
            # stores flag fields only.
            result = await collection.update_one(
                {"_id": FLAGS_ID}, {"$setOnInsert": DESIRED_FLAGS}, upsert=True
            )
            if result.upserted_id != FLAGS_ID:
                raise RuntimeError("APPLY BLOCKED: flags document was not inserted")
            report["exact_write_performed"] = {
                "collection": "app_config",
                "operation": "update_one upsert with $setOnInsert",
                "filter": {"_id": FLAGS_ID},
                "inserted_document": {"_id": FLAGS_ID, **DESIRED_FLAGS},
                "matched_count": result.matched_count,
                "modified_count": result.modified_count,
                "upserted_id": str(result.upserted_id),
            }
            report["collections_changed"] = ["app_config"]

        after_docs, after_flags, after_check_in = await read_state(collection)
        report["after_state"] = {
            "app_config_document_count": len(after_docs),
            "document_ids": sorted(str(doc.get("_id")) for doc in after_docs),
            "flags": public_document(after_flags),
            "location_check_in": public_document(after_check_in),
        }
        post_gates = {
            "location_check_in_exists_after": after_check_in is not None,
            "location_check_in_is_unchanged": public_document(after_check_in) == public_document(before_check_in),
            "flags_document_exists": after_flags is not None,
            "consumer_ordering_enabled_is_false": (after_flags or {}).get("consumer_ordering_enabled") is False,
            "consumer_premium_sales_enabled_is_true": (after_flags or {}).get("consumer_premium_sales_enabled") is True,
            "only_expected_document_id_was_added": set(str(doc.get("_id")) for doc in after_docs) - set(str(doc.get("_id")) for doc in before_docs) <= {FLAGS_ID},
        }
        report["post_write_validation"] = post_gates if args.apply else "NOT_APPLICABLE_DRY_RUN"
        report["acceptance_result"] = (
            "PASS" if args.apply and all(post_gates.values()) and not report["exceptions"]
            else "READY_FOR_APPLY" if not args.apply and all(gates.values())
            else "FAIL"
        )
        report["record_counts"] = {"app_config_before": len(before_docs), "app_config_after": len(after_docs)}
    except Exception as error:
        report["exceptions"].append({"kind": "operation_error", "message": str(error)})
        report["acceptance_result"] = "FAIL"
        raise
    finally:
        client.close()
        report["finished_at"] = now()
        Path(args.report).write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps({"report": args.report, "acceptance_result": report.get("acceptance_result"),
                          "collections_changed": report["collections_changed"]}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
