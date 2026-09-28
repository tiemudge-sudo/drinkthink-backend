#!/usr/bin/env python3
"""
DrinkThink canonical MongoDB migration inspector/backfill v2.

DRY-RUN IS THE DEFAULT.
--apply is intentionally blocked unless --confirm-apply CANONICAL_V2 is also supplied.

Changes from v1:
- canonical ingredient_id preserves existing string IDs (e.g. vodka__vodka)
- ingredient hierarchy migration supports string IDs
- glass exceptions are summarized by distinct legacy value rather than once/drink
- duplicate legacy drink IDs include source-document details
- dry-run builds deterministic preview IDs so reference validation is exact
- no recipe/capability data is guessed
"""
import argparse, asyncio, hashlib, json, os, re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING

VERSION = "canonical-v2"

def now(): return datetime.now(timezone.utc)

def normalize_name(value):
    value=(value or "").strip().lower()
    value=re.sub(r"[^a-z0-9]+"," ",value)
    return re.sub(r"\s+"," ",value).strip()

def canonical_id_for_legacy(legacy_id):
    # Deterministic bridge: idempotent across dry-run/apply and environments.
    digest=hashlib.sha256(f"drinkthink:legacy:{legacy_id}".encode()).hexdigest()[:16]
    return f"ckt_{digest}"

def score_object(d):
    return {"strong":d.get("dark"),"fancy":d.get("fancy"),
            "comfort":d.get("calm"),"party":d.get("celebrate"),
            "thirsty":d.get("thirsty")}

def canonical_cocktail(d,cid):
    return {
      "cocktail_id":cid,"legacy_drink_id":d["id"],"name":d.get("name",""),
      "normalized_name":normalize_name(d.get("name")),"category":d.get("category") or None,
      "instructions":d.get("instructions") or None,
      "human_ingredients":d.get("ingredients") or None,
      "shopping_tokens":d.get("shopping") or None,
      "alcohol_class":d.get("alcohol") or None,
      "glass_id":None,"legacy_glass":d.get("glass") or None,
      "main_ingredient_ids":[],"scores":score_object(d),"source":"curated",
      "admitted_from_review_id":None,"status":"active",
      "migration":{"source":"legacy_drinks","version":VERSION}
    }

def walk_tree(tree):
    categories=[]; ingredients=[]; problems=[]
    for ci,cat in enumerate(tree or []):
        cat_id=str(cat.get("id") or f"category_{ci+1}")
        categories.append({"category_id":cat_id,"name":cat.get("name") or cat_id,
                           "display_order":ci,"status":"active"})
        for primary in cat.get("primaries",[]) or []:
            pid=primary.get("id")
            if not isinstance(pid,str) or not pid.strip():
                problems.append({"kind":"invalid_ingredient_id","level":"primary",
                                 "source_id":pid,"name":primary.get("name")})
                continue
            ingredients.append({"ingredient_id":pid,"name":primary.get("name") or pid,
                "normalized_name":normalize_name(primary.get("name")),"category_id":cat_id,
                "parent_ingredient_id":None,"ingredient_type":"primary","aliases":[],"status":"active"})
            for item in primary.get("items",[]) or []:
                iid=item.get("id")
                if not isinstance(iid,str) or not iid.strip():
                    problems.append({"kind":"invalid_ingredient_id","level":"item",
                                     "source_id":iid,"name":item.get("name"),"parent_source_id":pid})
                    continue
                ingredients.append({"ingredient_id":iid,"name":item.get("name") or iid,
                    "normalized_name":normalize_name(item.get("name")),"category_id":cat_id,
                    "parent_ingredient_id":pid,"ingredient_type":"item","aliases":[],"status":"active"})
    return categories,ingredients,problems

class Migration:
    def __init__(self,db,apply=False):
        self.db=db; self.apply=apply
        self.report={"mode":"APPLY" if apply else "DRY_RUN","version":VERSION,
                     "started_at":now().isoformat(),"counts":Counter(),
                     "exceptions":[],"summaries":{},"validation":{}}
        self.bridge={}
    def count(self,k,n=1): self.report["counts"][k]+=n

    async def indexes(self):
        specs=[
          ("cocktails",[("cocktail_id",1)],{"unique":True}),
          ("cocktails",[("legacy_drink_id",1)],{"unique":True,"sparse":True}),
          ("cocktails",[("normalized_name",1)],{}),
          ("ingredient_categories",[("category_id",1)],{"unique":True}),
          ("ingredients",[("ingredient_id",1)],{"unique":True}),
          ("ingredients",[("parent_ingredient_id",1)],{}),
          ("ingredients",[("category_id",1)],{}),
          ("cocktail_ingredients",[("cocktail_id",1)],{}),
          ("cocktail_ingredients",[("ingredient_id",1)],{}),
          ("organizations",[("organization_id",1)],{"unique":True}),
          ("locations",[("location_id",1)],{"unique":True}),
          ("location_settings",[("location_id",1)],{"unique":True}),
          ("location_inventory",[("location_id",1),("ingredient_id",1)],{"unique":True}),
          ("location_drinks",[("location_id",1),("cocktail_id",1)],{"unique":True}),
          ("pos_connections",[("pos_connection_id",1)],{"unique":True}),
          ("pos_catalog_items",[("pos_connection_id",1),("provider_item_id",1)],{"unique":True}),
          ("pos_ingredient_mappings",[("pos_catalog_item_id",1),("ingredient_id",1)],{"unique":True}),
          ("pos_drink_mappings",[("mapping_id",1)],{"unique":True}),
          ("pos_drink_mappings",[("pos_connection_id",1),("provider_drink_id",1)],{"unique":True}),
          ("pos_sync_log",[("sync_id",1)],{"unique":True}),
          ("drink_review_queue",[("review_id",1)],{"unique":True}),
          ("orders",[("order_id",1)],{"unique":True}),
          ("order_items",[("order_item_id",1)],{"unique":True}),
          ("order_events",[("event_id",1)],{"unique":True}),
          ("schema_migrations",[("migration_id",1)],{"unique":True})]
        self.count("indexes_planned",len(specs))
        if self.apply:
            for c,k,o in specs:
                await self.db[c].create_index(k,**o); self.count("indexes_created")

    async def ingredients(self):
        doc=await self.db.ingredients_tree.find_one({"_id":"tree"},{"_id":0})
        if not doc:
            self.report["exceptions"].append({"kind":"missing_source","collection":"ingredients_tree"}); return
        cats,ings,problems=walk_tree(doc.get("data",[]))
        self.count("ingredient_categories_found",len(cats))
        self.count("canonical_ingredients_found",len(ings))
        self.count("ingredient_id_exceptions",len(problems))
        self.report["exceptions"].extend(problems)
        ids=[x["ingredient_id"] for x in ings]
        dup=[k for k,v in Counter(ids).items() if v>1]
        if dup:self.report["exceptions"].append({"kind":"duplicate_ingredient_ids","values":dup})
        self.ingredient_ids=set(ids)
        if self.apply:
            ts=now()
            for x in cats:
                x["updated_at"]=ts
                await self.db.ingredient_categories.update_one({"category_id":x["category_id"]},
                    {"$set":x,"$setOnInsert":{"created_at":ts}},upsert=True)
            for x in ings:
                x["updated_at"]=ts
                await self.db.ingredients.update_one({"ingredient_id":x["ingredient_id"]},
                    {"$set":x,"$setOnInsert":{"created_at":ts}},upsert=True)

    async def cocktails(self):
        by_id=defaultdict(list); glasses=Counter()
        async for d in self.db.drinks.find({}):
            by_id[d.get("id")].append(d)
            if d.get("glass"): glasses[str(d["glass"])]+=1
        self.count("legacy_documents_found",sum(map(len,by_id.values())))
        for lid,rows in by_id.items():
            if not isinstance(lid,int):
                self.report["exceptions"].append({"kind":"invalid_legacy_drink_id","value":lid,
                    "documents":[{"_id":str(x.get("_id")),"name":x.get("name")} for x in rows]})
                continue
            if len(rows)>1:
                self.report["exceptions"].append({"kind":"duplicate_legacy_drink_id","value":lid,
                    "documents":[{"_id":str(x.get("_id")),"name":x.get("name"),
                                  "category":x.get("category"),"glass":x.get("glass")} for x in rows]})
                continue
            d=rows[0]
            existing=await self.db.cocktails.find_one({"legacy_drink_id":lid},{"_id":0,"cocktail_id":1})
            cid=existing["cocktail_id"] if existing else canonical_id_for_legacy(lid)
            self.bridge[lid]=cid
            self.count("unique_legacy_drinks_migratable")
            self.count("cocktails_existing" if existing else "cocktails_to_create")
            if d.get("ingredients") or d.get("shopping"):
                self.count("cocktails_requiring_recipe_resolution")
            if self.apply:
                row=canonical_cocktail(d,cid); ts=now(); row["updated_at"]=ts
                await self.db.cocktails.update_one({"legacy_drink_id":lid},
                    {"$set":row,"$setOnInsert":{"created_at":ts}},upsert=True)
        self.report["summaries"]["legacy_glass_values"]=[
            {"value":k,"drink_count":v} for k,v in sorted(glasses.items(),key=lambda x:(-x[1],x[0]))
        ]
        self.report["summaries"]["distinct_legacy_glass_values"]=len(glasses)

    async def user_refs(self):
        for name in ("favorites","blocked","pending_shares"):
            async for row in self.db[name].find({},{"_id":1,"drink_id":1,"cocktail_id":1}):
                if row.get("cocktail_id"): self.count(f"{name}_already_canonical"); continue
                lid=row.get("drink_id"); cid=self.bridge.get(lid)
                if not cid:
                    self.report["exceptions"].append({"kind":"orphan_or_ambiguous_drink_reference",
                        "collection":name,"drink_id":lid}); continue
                self.count(f"{name}_references_to_backfill")
                if self.apply: await self.db[name].update_one({"_id":row["_id"]},{"$set":{"cocktail_id":cid}})

    async def cupboard(self):
        valid=getattr(self,"ingredient_ids",set())
        async for row in self.db.user_cupboard.find({},{"_id":1,"user_id":1,"item_ids":1}):
            vals=row.get("item_ids",[]) or []; bad=[x for x in vals if x not in valid]
            self.count("cupboard_users_checked")
            self.count("cupboard_item_ids_checked",len(vals))
            self.count("cupboard_item_ids_valid",len(vals)-len(bad))
            if bad:self.report["exceptions"].append({"kind":"cupboard_unresolved_ids",
                "user_id":row.get("user_id"),"values":bad})
            # String IDs are already canonical; no rewrite is needed.

    async def validate(self):
        legacy=await self.db.drinks.count_documents({})
        canonical=await self.db.cocktails.count_documents({})
        dup_groups=sum(1 for e in self.report["exceptions"] if e["kind"]=="duplicate_legacy_drink_id")
        self.report["validation"]={
          "legacy_document_count":legacy,
          "unique_legacy_drinks_migratable":self.report["counts"].get("unique_legacy_drinks_migratable",0),
          "duplicate_legacy_id_groups":dup_groups,
          "canonical_cocktail_count_before_or_after_run":canonical,
          "expected_canonical_count_after_apply":self.report["counts"].get("unique_legacy_drinks_migratable",0),
          "ingredient_ids_preserved_as_strings":True,
          "safe_to_apply": dup_groups==0 and not any(e["kind"] in
              ("invalid_legacy_drink_id","duplicate_ingredient_ids","missing_source") for e in self.report["exceptions"])
        }

    async def run(self):
        if self.apply:
            await self.db.schema_migrations.update_one({"migration_id":"canonical_schema_v2"},
              {"$set":{"version":2,"status":"running","started_at":now()}},upsert=True)
        await self.indexes(); await self.ingredients(); await self.cocktails()
        await self.user_refs(); await self.cupboard(); await self.validate()
        self.report["counts"]=dict(self.report["counts"])
        self.report["completed_at"]=now().isoformat()
        self.report["exception_count"]=len(self.report["exceptions"])
        if self.apply:
            await self.db.schema_migrations.update_one({"migration_id":"canonical_schema_v2"},
              {"$set":{"status":"backfill_complete","completed_at":now(),
                       "counts":self.report["counts"],"validation":self.report["validation"],
                       "exception_count":self.report["exception_count"]}})
        return self.report

async def amain(a):
    load_dotenv(a.env_file)
    url=a.mongo_url or os.getenv("MONGO_URL"); name=a.db_name or os.getenv("DB_NAME")
    if not url or not name: raise SystemExit("MONGO_URL and DB_NAME are required.")
    if a.apply and a.confirm_apply!="CANONICAL_V2":
        raise SystemExit("--apply requires --confirm-apply CANONICAL_V2")
    client=AsyncIOMotorClient(url)
    try:
        report=await Migration(client[name],a.apply).run()
        Path(a.report).write_text(json.dumps(report,indent=2,default=str),encoding="utf-8")
        print(json.dumps({"mode":report["mode"],"version":VERSION,"report":a.report,
          "counts":report["counts"],"exception_count":report["exception_count"],
          "validation":report["validation"]},indent=2))
    finally: client.close()

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--apply",action="store_true")
    p.add_argument("--confirm-apply")
    p.add_argument("--env-file",default=".env")
    p.add_argument("--mongo-url"); p.add_argument("--db-name")
    p.add_argument("--report",default="migration-report-v2.json")
    asyncio.run(amain(p.parse_args()))
if __name__=="__main__": main()
