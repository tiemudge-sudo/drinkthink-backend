#!/usr/bin/env python3
"""
DrinkThink canonical MongoDB reset/migration v3.

Pre-production migration: user-domain test data may be reset.
Canonical cocktail IDs are newly assigned deterministic IDs based on source Mongo _id.
Legacy numeric drink IDs are provenance only and are NOT application identity.

Default = dry-run. Use --apply --confirm-apply RESET_CANONICAL_V3 to write.
No collection is physically dropped; canonical target collections are rebuilt by
delete_many({}) only when apply is explicitly confirmed.
"""
import argparse, asyncio, hashlib, json, os, re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

VERSION="canonical-reset-v3"

CANONICAL_GLASS_MAP={
 "cocktail glass":"cocktail","martini":"martini",
 "highball glass":"highball","collins glass":"collins",
 "shot glass":"shot","old-fashioned glass":"rocks",
 "hurricane glass":"hurricane","margarita glass":"margarita",
 "beer mug":"beer_mug","beer pilsner":"pilsner","pint glass":"pint",
 "champagne flute":"champagne_flute","champagne saucer":"coupe",
 "champagne tulip":"champagne_flute","coupe glass":"coupe",
 "white wine glass":"wine","red wine glass":"wine","wine goblet":"wine",
 "irish coffee cup":"irish_coffee","coffee mug":"coffee_mug","cup":"cup",
 "punch bowl":"punch_bowl","whiskey sour glass":"sour","sour glass":"sour",
 "parfait glass":"parfait","pina colada glass":"pina_colada",
 "brandy snifter":"snifter","pitcher":"pitcher","cordial glass":"cordial",
 "mason jar":"mason_jar","mug":"mug","pousse cafe glass":"pousse_cafe",
 "aperitif glass":"aperitif","bottle":"bottle","jug":"jug",
 "sherry glass":"sherry","test tube":"test_tube","bucket":"bucket",
 "cooler":"cooler","any glass":"any"
}
# Deliberately unresolved composite; report for explicit decision.
COMPOSITE_GLASS_VALUES={"shot glass | bottle"}

def now(): return datetime.now(timezone.utc)
def norm(v):
    v=(v or "").strip().lower()
    v=re.sub(r"\s+"," ",v)
    return v
def normalized_name(v):
    return re.sub(r"\s+"," ",re.sub(r"[^a-z0-9]+"," ",norm(v))).strip()
def cocktail_id(source_mongo_id):
    h=hashlib.sha256(f"drinkthink:cocktail:{source_mongo_id}".encode()).hexdigest()[:20]
    return f"ckt_{h}"

def walk_tree(tree):
    cats=[]; ings=[]; problems=[]
    for ci,cat in enumerate(tree or []):
        cid=str(cat.get("id") or f"category_{ci+1}")
        cats.append({"category_id":cid,"name":cat.get("name") or cid,
                     "display_order":ci,"status":"active"})
        for p in cat.get("primaries",[]) or []:
            pid=p.get("id")
            if not isinstance(pid,str) or not pid.strip():
                problems.append({"kind":"invalid_ingredient_id","value":pid}); continue
            ings.append({"ingredient_id":pid,"name":p.get("name") or pid,
              "normalized_name":normalized_name(p.get("name")),"category_id":cid,
              "parent_ingredient_id":None,"ingredient_type":"primary",
              "aliases":[],"status":"active"})
            for i in p.get("items",[]) or []:
                iid=i.get("id")
                if not isinstance(iid,str) or not iid.strip():
                    problems.append({"kind":"invalid_ingredient_id","value":iid}); continue
                ings.append({"ingredient_id":iid,"name":i.get("name") or iid,
                  "normalized_name":normalized_name(i.get("name")),"category_id":cid,
                  "parent_ingredient_id":pid,"ingredient_type":"item",
                  "aliases":[],"status":"active"})
    return cats,ings,problems

class Runner:
    def __init__(self,db,apply):
        self.db=db; self.apply=apply
        self.r={"mode":"APPLY" if apply else "DRY_RUN","version":VERSION,
                "started_at":now().isoformat(),"counts":Counter(),
                "exceptions":[],"glass_summary":{},"validation":{}}
    def c(self,k,n=1): self.r["counts"][k]+=n

    async def collect(self):
        tree=await self.db.ingredients_tree.find_one({"_id":"tree"},{"_id":0})
        if not tree: raise RuntimeError("ingredients_tree/_id=tree not found")
        self.cats,self.ings,problems=walk_tree(tree.get("data",[]))
        self.r["exceptions"].extend(problems)
        self.c("ingredient_categories",len(self.cats)); self.c("ingredients",len(self.ings))

        self.drinks=[]; glass_counts=Counter()
        async for d in self.db.drinks.find({}):
            self.drinks.append(d)
            if d.get("glass"): glass_counts[norm(d["glass"])]+=1
        self.c("source_drinks",len(self.drinks))
        unmapped={k:v for k,v in glass_counts.items()
                  if k not in CANONICAL_GLASS_MAP and k not in COMPOSITE_GLASS_VALUES}
        composites={k:v for k,v in glass_counts.items() if k in COMPOSITE_GLASS_VALUES}
        self.r["glass_summary"]={
          "distinct_normalized_values":len(glass_counts),
          "mapped_values":len([k for k in glass_counts if k in CANONICAL_GLASS_MAP]),
          "unmapped":unmapped,"composite_requires_decision":composites}
        if unmapped:self.r["exceptions"].append({"kind":"unmapped_glass_values","values":unmapped})
        if composites:self.r["exceptions"].append({"kind":"composite_glass_values","values":composites})

    async def rebuild(self):
        # Canonical knowledge collections are cleanly rebuilt because this is pre-production.
        targets=["cocktails","cocktail_ingredients","ingredient_categories","ingredients","glasses"]
        user_test=["favorites","blocked","pending_shares","user_cupboard"]
        if self.apply:
            for n in targets:
                await self.db[n].delete_many({})
            for n in user_test:
                await self.db[n].delete_many({})
            self.c("test_user_collections_reset",len(user_test))

        ts=now()
        glass_ids=sorted(set(CANONICAL_GLASS_MAP.values()))
        glasses=[{"glass_id":g,"name":g.replace("_"," ").title(),"status":"active"} for g in glass_ids]
        self.c("canonical_glasses",len(glasses))

        if self.apply:
            if self.cats: await self.db.ingredient_categories.insert_many([{**x,"created_at":ts,"updated_at":ts} for x in self.cats])
            if self.ings: await self.db.ingredients.insert_many([{**x,"created_at":ts,"updated_at":ts} for x in self.ings])
            if glasses: await self.db.glasses.insert_many(glasses)

        rows=[]
        for d in self.drinks:
            rawglass=norm(d.get("glass"))
            gid=CANONICAL_GLASS_MAP.get(rawglass)
            row={
              "cocktail_id":cocktail_id(str(d["_id"])),
              "name":d.get("name",""),"normalized_name":normalized_name(d.get("name")),
              "category":d.get("category") or None,
              "instructions":d.get("instructions") or None,
              "human_ingredients":d.get("ingredients") or None,
              "shopping_tokens":d.get("shopping") or None,
              "alcohol_class":d.get("alcohol") or None,
              "glass_id":gid,
              "main_ingredient_ids":[],
              "scores":{"strong":d.get("dark"),"fancy":d.get("fancy"),
                        "comfort":d.get("calm"),"party":d.get("celebrate"),
                        "thirsty":d.get("thirsty")},
              "source":"curated","status":"active",
              "migration":{"version":VERSION,"source_mongo_id":str(d["_id"]),
                           "legacy_drink_id":d.get("id"),"legacy_glass":d.get("glass")},
              "created_at":ts,"updated_at":ts}
            rows.append(row)
        self.c("cocktails_prepared",len(rows))
        if self.apply and rows: await self.db.cocktails.insert_many(rows,ordered=False)

        if self.apply:
            await self.db.cocktails.create_index("cocktail_id",unique=True)
            await self.db.cocktails.create_index("normalized_name")
            await self.db.ingredients.create_index("ingredient_id",unique=True)
            await self.db.ingredients.create_index("parent_ingredient_id")
            await self.db.ingredient_categories.create_index("category_id",unique=True)
            await self.db.glasses.create_index("glass_id",unique=True)

    async def validate(self):
        ids=[cocktail_id(str(d["_id"])) for d in self.drinks]
        self.r["validation"]={
          "source_drink_count":len(self.drinks),
          "prepared_cocktail_count":self.r["counts"].get("cocktails_prepared",0),
          "unique_new_cocktail_ids":len(set(ids)),
          "ingredient_count":len(self.ings),
          "ingredient_ids_unique":len({x["ingredient_id"] for x in self.ings})==len(self.ings),
          "all_source_drinks_receive_new_id":len(set(ids))==len(self.drinks),
          "glass_unmapped_count":sum(self.r["glass_summary"]["unmapped"].values()),
          "glass_composite_count":sum(self.r["glass_summary"]["composite_requires_decision"].values())
        }
        if self.apply:
            self.r["validation"]["db_cocktail_count"]=await self.db.cocktails.count_documents({})
            self.r["validation"]["db_ingredient_count"]=await self.db.ingredients.count_documents({})

    async def run(self):
        await self.collect(); await self.rebuild(); await self.validate()
        self.r["counts"]=dict(self.r["counts"]); self.r["completed_at"]=now().isoformat()
        self.r["exception_count"]=len(self.r["exceptions"])
        if self.apply:
            await self.db.schema_migrations.update_one({"migration_id":VERSION},
              {"$set":{"status":"complete","completed_at":now(),"report":self.r}},upsert=True)
        return self.r

async def amain(a):
    load_dotenv(a.env_file)
    url=a.mongo_url or os.getenv("MONGO_URL"); dbn=a.db_name or os.getenv("DB_NAME")
    if not url or not dbn: raise SystemExit("MONGO_URL and DB_NAME are required.")
    if a.apply and a.confirm_apply!="RESET_CANONICAL_V3":
        raise SystemExit("--apply requires --confirm-apply RESET_CANONICAL_V3")
    client=AsyncIOMotorClient(url)
    try:
        r=await Runner(client[dbn],a.apply).run()
        Path(a.report).write_text(json.dumps(r,indent=2,default=str),encoding="utf-8")
        print(json.dumps({"mode":r["mode"],"report":a.report,"counts":r["counts"],
                          "exceptions":r["exception_count"],"validation":r["validation"]},indent=2))
    finally: client.close()

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--apply",action="store_true"); p.add_argument("--confirm-apply")
    p.add_argument("--env-file",default=".env"); p.add_argument("--mongo-url"); p.add_argument("--db-name")
    p.add_argument("--report",default="migration-report-v3.json")
    asyncio.run(amain(p.parse_args()))
if __name__=="__main__": main()
