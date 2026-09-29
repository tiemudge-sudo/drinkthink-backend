#!/usr/bin/env python3
"""DrinkThink cocktail_ingredients v3.

Contract-driven migration:
  drinks_clean_100pct.csv ingredient_ids (authoritative recipe assignments)
    -> ingredient_id_merge_map.json redirect when present
    -> validate resulting integer ID against All-ingredients.csv
    -> validate ingredient_lookup.json contains the canonical ingredient identity
    -> cocktail_ingredients

No recipe-text parsing, fuzzy matching, taxonomy invention, or source cleansing.
Dry-run is the default. Apply is blocked unless every source ingredient reference validates.
"""
import argparse, ast, asyncio, csv, json, os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

VERSION='cocktail-ingredients-v3'
CONFIRM='BUILD_COCKTAIL_INGREDIENTS_V3'

def utcnow(): return datetime.now(timezone.utc)

def parse_ids(raw):
    if raw is None: return []
    if isinstance(raw, list): vals=raw
    else:
        s=str(raw).strip()
        if not s or s.lower()=='nan': return []
        vals=ast.literal_eval(s)
    if not isinstance(vals,list): raise ValueError(f'ingredient_ids is not a list: {raw!r}')
    return [int(x) for x in vals]

class Runner:
    def __init__(self, db, root, apply):
        self.db=db; self.root=root; self.apply=apply
        self.report={'version':VERSION,'mode':'APPLY' if apply else 'DRY_RUN','started_at':utcnow().isoformat(),
                     'source_contract':{'recipes':'data/drinks_clean_100pct.csv','ingredient_master':'data/All-ingredients.csv',
                     'surface_lookup':'data/ingredient_lookup.json','merge_map':'data/ingredient_id_merge_map.json'},
                     'counts':Counter(),'exceptions':[],'validation':{}}

    def load_sources(self):
        with open(self.root/'data'/'ingredient_id_merge_map.json',encoding='utf-8') as f:
            obj=json.load(f); self.merge={int(k):int(v) for k,v in obj['map'].items()}
        with open(self.root/'data'/'ingredient_lookup.json',encoding='utf-8') as f:
            lookup=json.load(f); lookup=lookup.get('map',lookup) if isinstance(lookup,dict) else lookup
        self.lookup_ids=set()
        if isinstance(lookup,dict):
            for v in lookup.values():
                try:self.lookup_ids.add(int(v))
                except (TypeError,ValueError):pass
        else: raise RuntimeError('ingredient_lookup.json must be an object/map')
        with open(self.root/'data'/'All-ingredients.csv',newline='',encoding='utf-8-sig') as f:
            master=list(csv.DictReader(f))
        self.master={int(r['ID']):r for r in master}
        with open(self.root/'data'/'drinks_clean_100pct.csv',newline='',encoding='utf-8-sig') as f:
            self.recipes=list(csv.DictReader(f))
        self.report['counts'].update({'authoritative_recipe_rows':len(self.recipes),'ingredient_master_rows':len(self.master),
                                      'merge_map_entries':len(self.merge),'ingredient_lookup_keys':len(lookup),
                                      'ingredient_lookup_distinct_ids':len(self.lookup_ids)})

    def resolve_source_id(self, sid):
        # Contract: apply merge redirect when defined. A newer stable All-ingredients ID may
        # post-date the merge-map artifact; it is accepted only when it exists in the master
        # AND ingredient_lookup independently references that same canonical ID.
        if sid in self.merge: return self.merge[sid], 'merge_map'
        if sid in self.master and sid in self.lookup_ids: return sid, 'master_plus_lookup_identity'
        return None, 'unresolved'

    async def run(self):
        self.load_sources()
        # Current canonical DB compatibility: legacy bridge may be top-level per locked schema
        # or nested under migration from the already-applied reset migration.
        cocktails=await self.db.cocktails.find({}, {'_id':0,'cocktail_id':1,'legacy_drink_id':1,'migration.legacy_drink_id':1}).to_list(None)
        by_legacy=defaultdict(list)
        for c in cocktails:
            lid=c.get('legacy_drink_id')
            if lid is None: lid=(c.get('migration') or {}).get('legacy_drink_id')
            try: by_legacy[int(lid)].append(c['cocktail_id'])
            except (TypeError,ValueError,KeyError): pass
        db_ing_docs=await self.db.ingredients.find({}, {'_id':0,'ingredient_id':1}).to_list(None)
        db_ing_ids={d.get('ingredient_id') for d in db_ing_docs}
        db_int_ids={int(x) for x in db_ing_ids if isinstance(x,int) or (isinstance(x,str) and x.isdigit())}
        self.report['counts']['db_cocktails']=len(cocktails); self.report['counts']['db_ingredients']=len(db_ing_docs)
        self.report['counts']['db_integer_ingredient_ids']=len(db_int_ids)

        relationships=[]; source_refs=0; resolved_refs=0; missing_db=set(); missing_master=set(); lookup_gap=set(); merge_redirects=0
        recipe_failures=0; seen_recipe_ids=set(); duplicate_recipe_ids=[]
        for r in self.recipes:
            try: legacy_id=int(r['id'])
            except Exception:
                self.report['exceptions'].append({'kind':'invalid_recipe_id','value':r.get('id'),'name':r.get('d_name')}); recipe_failures+=1; continue
            if legacy_id in seen_recipe_ids: duplicate_recipe_ids.append(legacy_id)
            seen_recipe_ids.add(legacy_id)
            try: src_ids=parse_ids(r.get('ingredient_ids'))
            except Exception as e:
                self.report['exceptions'].append({'kind':'invalid_ingredient_ids','legacy_drink_id':legacy_id,'name':r.get('d_name'),'error':str(e)}); recipe_failures+=1; continue
            source_refs += len(src_ids)
            cocktail_ids=by_legacy.get(legacy_id,[])
            errs=[]; resolved=[]
            if len(cocktail_ids)!=1: errs.append({'reason':'cocktail_bridge_count','count':len(cocktail_ids)})
            for seq,sid in enumerate(src_ids,1):
                rid,method=self.resolve_source_id(sid)
                if rid is None:
                    errs.append({'source_ingredient_id':sid,'reason':'not_in_merge_map_or_validated_master_lookup_identity'})
                    if sid not in self.master: missing_master.add(sid)
                    if sid not in self.lookup_ids: lookup_gap.add(sid)
                    continue
                if method=='merge_map' and rid!=sid: merge_redirects+=1
                if rid not in self.master:
                    errs.append({'source_ingredient_id':sid,'resolved_ingredient_id':rid,'reason':'resolved_id_not_in_All-ingredients'}); missing_master.add(rid); continue
                if rid not in self.lookup_ids:
                    errs.append({'source_ingredient_id':sid,'resolved_ingredient_id':rid,'reason':'resolved_id_not_referenced_by_ingredient_lookup'}); lookup_gap.add(rid); continue
                if rid not in db_int_ids:
                    errs.append({'source_ingredient_id':sid,'resolved_ingredient_id':rid,'reason':'canonical_db_ingredient_id_missing'}); missing_db.add(rid); continue
                resolved.append((seq,sid,rid,method)); resolved_refs+=1
            if errs:
                recipe_failures+=1
                self.report['exceptions'].append({'kind':'recipe_incomplete','legacy_drink_id':legacy_id,'name':r.get('d_name'),
                                                  'source_ingredient_count':len(src_ids),'errors':errs})
                continue
            cid=cocktail_ids[0]
            # Preserve each authoritative recipe position. If two retired IDs merge to one ID,
            # sequence keeps both recipe relationships distinct under the locked unique index.
            for seq,sid,rid,method in resolved:
                relationships.append({'cocktail_id':cid,'ingredient_id':rid,'amount':None,'unit':None,
                                      'role':'ingredient','required':True,'sequence':seq,'notes':None,
                                      'migration':{'version':VERSION,'source_ingredient_id':sid,'resolution':method}})

        self.report['counts'].update({'source_ingredient_references':source_refs,'validated_ingredient_references':resolved_refs,
          'merge_redirected_references':merge_redirects,'recipes_fully_validated':len(self.recipes)-recipe_failures,
          'recipes_with_failures':recipe_failures,'cocktail_ingredient_rows_prepared':len(relationships)})
        self.report['validation']={
          'authoritative_recipe_ids_unique':not duplicate_recipe_ids,
          'duplicate_recipe_ids':sorted(set(duplicate_recipe_ids)),
          'all_source_ingredient_references_validated':resolved_refs==source_refs,
          'ingredient_reference_coverage_pct':round(100*resolved_refs/source_refs,4) if source_refs else 100.0,
          'all_authoritative_recipes_fully_validated':recipe_failures==0,
          'all_written_ingredient_ids_exist_in_master':all(x['ingredient_id'] in self.master for x in relationships),
          'all_written_ingredient_ids_exist_in_db':all(x['ingredient_id'] in db_int_ids for x in relationships),
          'missing_db_ingredient_ids':sorted(missing_db), 'missing_master_ingredient_ids':sorted(missing_master),
          'ingredient_lookup_gaps':sorted(lookup_gap),
          'apply_gate_passed': recipe_failures==0 and resolved_refs==source_refs and not duplicate_recipe_ids
        }
        if self.apply:
            if not self.report['validation']['apply_gate_passed']:
                raise RuntimeError('APPLY BLOCKED: dry-run acceptance criteria are not 100%. Review report.')
            cids=sorted({x['cocktail_id'] for x in relationships})
            await self.db.cocktail_ingredients.delete_many({'cocktail_id':{'$in':cids}})
            if relationships: await self.db.cocktail_ingredients.insert_many(relationships,ordered=True)
            await self.db.cocktail_ingredients.create_index([('cocktail_id',1),('ingredient_id',1),('role',1),('sequence',1)],unique=True)
            await self.db.cocktail_ingredients.create_index('ingredient_id'); await self.db.cocktail_ingredients.create_index('cocktail_id')
        self.report['finished_at']=utcnow().isoformat()
        self.report['counts']=dict(self.report['counts'])
        return self.report

async def main():
    p=argparse.ArgumentParser(); p.add_argument('--apply',action='store_true'); p.add_argument('--confirm-apply'); p.add_argument('--report',default='cocktail-ingredients-report-v3.json')
    a=p.parse_args();
    if a.apply and a.confirm_apply!=CONFIRM: raise SystemExit(f'Apply requires --confirm-apply {CONFIRM}')
    load_dotenv(); uri=os.getenv('MONGO_URL') or os.getenv('MONGODB_URI'); dbname=os.getenv('DB_NAME') or os.getenv('MONGO_DB_NAME')
    if not uri: raise SystemExit('MONGO_URL or MONGODB_URI is required')
    client=AsyncIOMotorClient(uri); db=client[dbname] if dbname else client.get_default_database()
    if db is None: raise SystemExit('DB_NAME is required when Mongo URI has no default database')
    root=Path(__file__).resolve().parents[1]
    try: report=await Runner(db,root,a.apply).run()
    finally: client.close()
    Path(a.report).write_text(json.dumps(report,indent=2,default=str),encoding='utf-8')
    print(json.dumps({'report':a.report,'counts':report['counts'],'validation':report['validation']},indent=2))
if __name__=='__main__': asyncio.run(main())
