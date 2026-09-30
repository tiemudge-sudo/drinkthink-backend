#!/usr/bin/env python3
"""Read-only canonical Master Drinks extraction from DrinkThinkv0.cocktails.

One ordered MongoDB find() result set is materialized once and is the sole
source for JSON, CSV, and the validation report. This tool has no write mode.
"""
import argparse
import asyncio
import csv
import hashlib
import json
import os
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from bson import json_util
from bson.objectid import ObjectId
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

ROOT = Path(__file__).resolve().parents[1]
COLLECTION, CANONICAL_ID = "cocktails", "cocktail_id"
FILTER_TYPES = {"cocktail_id": str, "status": str, "category": str,
                "alcohol_class": str, "glass_id": str, "source": str,
                "legacy_drink_id": int}
MAX_SAMPLE = 5


def now(): return datetime.now(timezone.utc).isoformat()
def digest(value): return hashlib.sha256(value.encode("utf-8")).hexdigest()
def ejson(value, indent=None): return json_util.dumps(value, json_options=json_util.CANONICAL_JSON_OPTIONS, indent=indent)
def scalar(value): return value is None or isinstance(value, (str, int, float, bool, datetime))
def csv_value(value): return "" if value is None else (value.astimezone(timezone.utc).isoformat() if isinstance(value, datetime) else str(value))


def query_for(args):
    if args.id and (args.filter_field or args.filter_value): raise SystemExit("Use --id or one filter pair, not both")
    if args.id: return {CANONICAL_ID: args.id}, {CANONICAL_ID: args.id}
    if args.filter_field is None and args.filter_value is None: return {}, "FULL_COLLECTION"
    if not args.filter_field or args.filter_value is None: raise SystemExit("--filter-field and --filter-value must be supplied together")
    kind = FILTER_TYPES[args.filter_field]
    try: value = kind(args.filter_value)
    except ValueError: raise SystemExit(f"Invalid value for {args.filter_field}")
    return {args.filter_field: value}, {args.filter_field: value}


def structure(records):
    fields = {}
    for record in records:
        for key, value in record.items():
            entry = fields.setdefault(key, {"present_count": 0, "types": Counter()})
            entry["present_count"] += 1; entry["types"][type(value).__name__] += 1
    return {key: {"present_count": val["present_count"], "types": dict(val["types"])} for key, val in sorted(fields.items())}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--report", default="master-drinks-extraction-report.json")
    parser.add_argument("--json-output", default="original.json")
    parser.add_argument("--csv-output", default="original.csv")
    parser.add_argument("--id")
    parser.add_argument("--filter-field", choices=sorted(FILTER_TYPES))
    parser.add_argument("--filter-value")
    args = parser.parse_args()
    load_dotenv(ROOT / args.env_file)
    mongo_url, db_name = os.getenv("MONGO_URL"), os.getenv("DB_NAME")
    if not mongo_url or not db_name: raise SystemExit("MONGO_URL and DB_NAME are required")
    query, query_label = query_for(args)
    report = {"tool": "extract-master-drinks-v1", "mode": "READ_ONLY", "extraction_id": str(uuid.uuid4()), "started_at": now(),
              "source_environment_assumptions": {"environment": "Railway backend terminal", "database": db_name, "collection": COLLECTION, "query": query_label, "sort": {CANONICAL_ID: 1}},
              "canonical_record_identifier": CANONICAL_ID, "collections_changed": [], "exceptions": []}
    client = AsyncIOMotorClient(mongo_url, appname="extract-master-drinks-v1")
    try:
        # The only MongoDB operation. JSON and CSV originate from these records.
        records = await client[db_name][COLLECTION].find(query).sort(CANONICAL_ID, 1).to_list(length=None)
    finally: client.close()
    ids = [record.get(CANONICAL_ID) for record in records]
    missing = [index for index, value in enumerate(ids) if not isinstance(value, str) or not value]
    duplicate = [key for key, count in Counter(ids).items() if count > 1]
    bad_object_ids = [record.get(CANONICAL_ID) for record in records if not isinstance(record.get("_id"), ObjectId)]
    if missing: report["exceptions"].append({"kind": "missing_or_invalid_canonical_id", "row_indexes": missing[:100]})
    if duplicate: report["exceptions"].append({"kind": "duplicate_canonical_id", "cocktail_ids": duplicate[:100]})
    if bad_object_ids: report["exceptions"].append({"kind": "non_objectid_mongo_id", "cocktail_ids": bad_object_ids[:100]})
    all_fields = sorted({field for record in records for field in record})
    complex_fields = [field for field in all_fields if any(field in record and not scalar(record[field]) for record in records)]
    scalar_fields = [field for field in all_fields if field not in complex_fields and field not in {"_id", CANONICAL_ID}]
    columns = [CANONICAL_ID, "mongo_object_id", *scalar_fields]
    rows = [{CANONICAL_ID: record.get(CANONICAL_ID), "mongo_object_id": str(record.get("_id")), **{field: csv_value(record.get(field)) for field in scalar_fields}} for record in records]
    json_text = ejson(records, indent=2) + "\n"
    json_path, csv_path, report_path = Path(args.json_output), Path(args.csv_output), Path(args.report)
    for path in (json_path, csv_path, report_path): path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json_text, encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns); writer.writeheader(); writer.writerows(rows)
    checks = {"single_mongodb_result_set_used_for_json_and_csv": True, "json_and_csv_counts_match": len(records) == len(rows), "canonical_ids_present_and_unique": not missing and not duplicate, "mongo_object_ids_preserved_in_json": not bad_object_ids, "mongodb_mutations_performed": 0}
    report.update({"record_counts": {"mongo_result_set": len(records), "json_records": len(records), "csv_rows": len(rows)}, "field_structure": structure(records),
                   "csv": {"columns": columns, "complex_fields_excluded_from_csv": complex_fields, "round_trip_policy": "Nested objects and arrays remain only in original.json."},
                   "outputs": {"original_json": str(json_path), "original_csv": str(csv_path), "json_sha256": digest(json_text), "result_set_extended_json_sha256": digest(ejson(records))},
                   "representative_samples": [{CANONICAL_ID: rec.get(CANONICAL_ID), "mongo_object_id": str(rec.get("_id")), "name": rec.get("name"), "keys": sorted(rec)} for rec in records[:MAX_SAMPLE]], "validation": checks})
    required_gates = [value for key, value in checks.items() if key != "mongodb_mutations_performed"]
    report["acceptance_result"] = "PASS" if all(required_gates) and checks["mongodb_mutations_performed"] == 0 and not report["exceptions"] else "FAIL"; report["finished_at"] = now()
    report_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(report_path), "acceptance_result": report["acceptance_result"], "record_count": len(records), "json_output": str(json_path), "csv_output": str(csv_path)}, indent=2))
    if report["acceptance_result"] != "PASS": raise SystemExit("Extraction validation failed; inspect the report")

if __name__ == "__main__": asyncio.run(main())
