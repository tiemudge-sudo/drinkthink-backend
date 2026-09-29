#!/usr/bin/env python3
import argparse, asyncio, ast, csv, hashlib, json, os, re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

VERSION='master-canonical-correction-v2'
CONFIRM='MASTER_CANONICAL_CORRECTION_V2'
ROOT=Path(__file__).resolve().parents[1]

# Explicit repair of four corrupted bridge collisions produced by canonical-reset-v3.
# These are document identities, not name-match rules. Runtime/master lookup remains legacy_drink_id only.
BRIDGE_REPAIRS = {
    1: {'keep': 'ckt_fa85b1610cf1c9037a6a', 'clear': ['ckt_2c601e519114bb8da094']},
    2: {'keep': 'ckt_322c1edca869513e2995', 'clear': ['ckt_54b0537b291c8b5bcc79']},
    5: {'keep': 'ckt_09b83c71abc3b22b1683', 'clear': ['ckt_4b26cfb5619351939abb']},
    7: {'keep': 'ckt_18c8ee0de51e30ac38bf', 'clear': ['ckt_e9f991046ec2717349a4']},
}

def now(): return datetime.now(timezone.utc)
def norm(s): return re.sub(r'\s+',' ',str(s or '').strip().lower())
def slug(s): return re.sub(r'[^a-z0-9]+','_',norm(s)).strip('_')
def normalized_name(s): return re.sub(r'\s+',' ',re.sub(r'[^a-z0-9]+',' ',norm(s))).strip()
def new_cocktail_id(master_id):
    h=hashlib.sha256(f'drinkthink:master:{master_id}'.encode()).hexdigest()[:20]
    return f'ckt_{h}'

def parse_ids(v):
    if v is None or str(v).strip()=='' or str(v).strip().lower()=='nan': return []
    x=ast.literal_eval(str(v))
    if not isinstance(x,list): raise ValueError(f'ingredient_ids not list: {v!r}')
    return [int(i) for i in x]

def load_json(name): return json.loads((ROOT/'data'/name).read_text(encoding='utf-8'))
def load_csv(name):
    with (ROOT/'data'/name).open(encoding='utf-8-sig',newline='') as f: return list(csv.DictReader(f))

def load_master():
    ing_rows=load_csv('All-ingredients.csv')
    drink_rows=load_csv('drinks_clean_100pct.csv')
    lookup=load_json('ingredient_lookup.json')
    mm=load_json('ingredient_id_merge_map.json')
    merge=mm.get('map',mm)
    merge={int(k):int(v) for k,v in merge.items()}
    master={int(r['ID']):r for r in ing_rows}
    aliases=defaultdict(list)
    for k,v in lookup.items(): aliases[int(v)].append(k)
    return ing_rows,drink_rows,lookup,merge,master,aliases

def resolve(i,merge): return merge.get(int(i),int(i))

class Runner:
    def __init__(self,db,apply):
        self.db=db; self.apply=apply
        self.r={'version':VERSION,'mode':'APPLY' if apply else 'DRY_RUN','started_at':now().isoformat(),
                'source':'DrinkThink_db.zip master generation','counts':Counter(),'exceptions':[],
                'bridge':{},'ingredients':{},'recipes':{},'apply_gate_passed':False}
    def c(self,k,n=1): self.r['counts'][k]+=n
    async def collect(self):
        self.ing_rows,self.drinks,self.lookup,self.merge,self.master,self.aliases=load_master()
        self.c('master_cocktails',len(self.drinks)); self.c('master_ingredients',len(self.ing_rows))
        self.c('lookup_keys',len(self.lookup)); self.c('merge_map_entries',len(self.merge))
        ids=[int(r['id']) for r in self.drinks]
        if len(ids)!=len(set(ids)): self.r['exceptions'].append({'kind':'duplicate_master_drink_ids'})
        self.current=[]
        async for d in self.db.cocktails.find({}): self.current.append(d)
        self.c('db_cocktails_before',len(self.current))
        self.current_ingredients=[]
        async for d in self.db.ingredients.find({}): self.current_ingredients.append(d)
        self.c('db_ingredients_before',len(self.current_ingredients))

    async def plan_bridge(self):
        by_top=defaultdict(list); by_nested=defaultdict(list)
        for d in self.current:
            if isinstance(d.get('legacy_drink_id'),int): by_top[d['legacy_drink_id']].append(d)
            n=(d.get('migration') or {}).get('legacy_drink_id')
            if isinstance(n,int): by_nested[n].append(d)
        plans=[]; counts=Counter()
        for row in self.drinks:
            mid=int(row['id']); candidates=by_top.get(mid,[])
            method='top_level_legacy_drink_id'
            if not candidates:
                candidates=by_nested.get(mid,[]); method='migration_legacy_drink_id'
            if len(candidates)>1:
                repair=BRIDGE_REPAIRS.get(mid)
                if repair:
                    by_cid={d.get('cocktail_id'):d for d in candidates}
                    expected={repair['keep'], *repair['clear']}
                    actual=set(by_cid)
                    if repair['keep'] in by_cid and expected.issubset(actual):
                        d=by_cid[repair['keep']]
                        plans.append((row,d['cocktail_id'],'explicit_bridge_repair',False))
                        counts['explicit_bridge_repair']+=1
                        continue
                    self.r['exceptions'].append({'kind':'bridge_repair_mismatch','master_id':mid,'expected_cocktail_ids':sorted(expected),'actual_cocktail_ids':sorted(actual)})
                    counts['repair_mismatch']+=1; continue
                self.r['exceptions'].append({'kind':'ambiguous_bridge','master_id':mid,'count':len(candidates),'method':method})
                counts['ambiguous']+=1; continue
            if len(candidates)==1:
                d=candidates[0]; plans.append((row,d['cocktail_id'],method,False)); counts[method]+=1
            else:
                cid=new_cocktail_id(mid); plans.append((row,cid,'create_from_master',True)); counts['create_from_master']+=1
        self.bridge_plans=plans
        self.r['bridge']=dict(counts)
        self.c('master_rows_bridged_or_creatable',len(plans))

    def plan_ingredients(self):
        rows=[]; category_names=[]
        for r in self.ing_rows:
            iid=int(r['ID']); cat=(r.get('Primary Category') or '').strip() or 'Uncategorized'
            if cat not in category_names: category_names.append(cat)
            rows.append({'ingredient_id':iid,'name':(r.get('All ingredients') or '').strip(),
              'normalized_name':normalized_name(r.get('All ingredients')),'category_id':slug(cat),
              'parent_ingredient_id':None,'ingredient_type':(r.get('subCategory') or '').strip() or None,
              'aliases':sorted(set(self.aliases.get(iid,[]))), 'status':'active',
              'primary_ingredient':(r.get('Primary Ingredient') or '').strip() or None,
              'primary_category':cat,'subcategory':(r.get('subCategory') or '').strip() or None})
        self.ingredient_rows=rows
        self.category_rows=[{'category_id':slug(n),'name':n,'display_order':i,'status':'active'} for i,n in enumerate(category_names)]
        self.r['ingredients']={'prepared':len(rows),'categories':len(self.category_rows),
          'integer_ids':all(isinstance(x['ingredient_id'],int) for x in rows),
          'unique_ids':len({x['ingredient_id'] for x in rows})==len(rows)}

    def plan_recipes(self):
        valid_ids={x['ingredient_id'] for x in self.ingredient_rows}; rel=[]; failures=[]; refs=0; redirected=0
        for row,cid,method,is_new in self.bridge_plans:
            src=parse_ids(row.get('ingredient_ids')); refs+=len(src); local=[]
            for seq,sid in enumerate(src,1):
                rid=resolve(sid,self.merge)
                if rid!=sid: redirected+=1
                if rid not in valid_ids:
                    failures.append({'master_id':int(row['id']),'cocktail_id':cid,'source_ingredient_id':sid,'resolved_ingredient_id':rid,'reason':'not_in_master_ingredients'})
                    continue
                local.append({'cocktail_id':cid,'ingredient_id':rid,'amount':None,'unit':None,
                              'role':'base' if seq==1 else 'ingredient','required':True,
                              'sequence':seq,'notes':None})
            if len(local)!=len(src): continue
            rel.extend(local)
        self.recipe_rows=rel
        self.r['recipes']={'source_ingredient_references':refs,'prepared_relationships':len(rel),
          'merge_redirected_references':redirected,'failures':len(failures)}
        if failures: self.r['exceptions'].append({'kind':'recipe_resolution_failures','count':len(failures),'sample':failures[:50]})

    def master_cocktail_doc(self,row,cid,ts):
        def f(k):
            v=row.get(k); return None if v is None or str(v).strip()=='' else v
        def num(k):
            try: return float(row[k]) if str(row.get(k,'')).strip() else None
            except: return None
        src=parse_ids(row.get('ingredient_ids')); resolved=[resolve(i,self.merge) for i in src]
        return {'cocktail_id':cid,'legacy_drink_id':int(row['id']),'name':row.get('d_name',''),
          'normalized_name':normalized_name(row.get('d_name')),'category':f('d_cat'),'instructions':f('d_instructions'),
          'human_ingredients':f('d_ingredients'),'shopping_tokens':f('d_shopping'),'alcohol_class':f('d_alcohol'),
          'glass_id':None,'main_ingredient_ids':resolved[:1],
          'scores':{'strong':num('Dark2'),'fancy':num('Fancy2'),'comfort':num('Calm2'),'party':num('Celebrate2'),'thirsty':num('Thirsty2')},
          'source':'curated','admitted_from_review_id':None,'status':'active','created_at':ts,'updated_at':ts,
          'migration':{'version':VERSION,'master_id':int(row['id']),'legacy_glass':f('d_glass')}}

    async def apply_changes(self):
        ts=now()
        # Replace canonical ingredient ontology from the declared master generation.
        await self.db.ingredients.delete_many({}); await self.db.ingredient_categories.delete_many({})
        if self.category_rows: await self.db.ingredient_categories.insert_many([{**x,'created_at':ts,'updated_at':ts} for x in self.category_rows])
        if self.ingredient_rows: await self.db.ingredients.insert_many([{**x,'created_at':ts,'updated_at':ts} for x in self.ingredient_rows])
        # Clear only the four explicitly identified corrupt nested bridge values.
        # Do not delete or deactivate the unrelated cocktails themselves.
        for mid,repair in BRIDGE_REPAIRS.items():
            for bad_cid in repair['clear']:
                await self.db.cocktails.update_one(
                    {'cocktail_id':bad_cid, 'migration.legacy_drink_id':mid},
                    {'$unset':{'migration.legacy_drink_id':''}, '$set':{'updated_at':ts}}
                )
        # Bridge exact known rows; create only where contract authorizes creation from master.
        for row,cid,method,is_new in self.bridge_plans:
            mid=int(row['id'])
            if is_new:
                await self.db.cocktails.insert_one(self.master_cocktail_doc(row,cid,ts))
            else:
                await self.db.cocktails.update_one({'cocktail_id':cid},{'$set':{'legacy_drink_id':mid,'updated_at':ts}})
        # Full replace recipes only for the master cocktail IDs in this batch.
        cids=[cid for _,cid,_,_ in self.bridge_plans]
        await self.db.cocktail_ingredients.delete_many({'cocktail_id':{'$in':cids}})
        if self.recipe_rows: await self.db.cocktail_ingredients.insert_many(self.recipe_rows,ordered=False)
        # main_ingredient_ids follows the first/base ingredient produced by the companion contract.
        bycid=defaultdict(list)
        for x in self.recipe_rows:
            if x['role']=='base': bycid[x['cocktail_id']].append((x['sequence'],x['ingredient_id']))
        for cid,v in bycid.items():
            vals=[i for _,i in sorted(v)]
            await self.db.cocktails.update_one({'cocktail_id':cid},{'$set':{'main_ingredient_ids':vals,'updated_at':ts}})
        await self.db.ingredients.create_index('ingredient_id',unique=True)
        await self.db.ingredients.create_index('category_id'); await self.db.ingredients.create_index('normalized_name')
        await self.db.ingredient_categories.create_index('category_id',unique=True)
        await self.db.cocktails.create_index('cocktail_id',unique=True)
        await self.db.cocktails.create_index('legacy_drink_id',unique=True,sparse=True)
        await self.db.cocktail_ingredients.create_index([('cocktail_id',1),('ingredient_id',1),('role',1),('sequence',1)],unique=True)
        await self.db.cocktail_ingredients.create_index('cocktail_id'); await self.db.cocktail_ingredients.create_index('ingredient_id')

    async def validate(self):
        ambiguous=sum(1 for e in self.r['exceptions'] if e.get('kind') in ('ambiguous_bridge','bridge_repair_mismatch'))
        recipe_fail=self.r['recipes'].get('failures',0)
        expected_refs=self.r['recipes'].get('source_ingredient_references',0)
        prepared=self.r['recipes'].get('prepared_relationships',0)
        self.r['apply_gate_passed']=(ambiguous==0 and recipe_fail==0 and expected_refs==prepared and len(self.bridge_plans)==len(self.drinks))
        if self.apply:
            self.r['post_apply']={'db_integer_ingredient_ids':await self.db.ingredients.count_documents({'ingredient_id':{'$type':'int'}}),
              'db_ingredients':await self.db.ingredients.count_documents({}),
              'master_bridged':await self.db.cocktails.count_documents({'legacy_drink_id':{'$in':[int(r['id']) for r in self.drinks]}}),
              'cocktail_ingredient_rows_for_master':await self.db.cocktail_ingredients.count_documents({'cocktail_id':{'$in':[cid for _,cid,_,_ in self.bridge_plans]}})}

    async def run(self):
        await self.collect(); await self.plan_bridge(); self.plan_ingredients(); self.plan_recipes(); await self.validate()
        if self.apply:
            if not self.r['apply_gate_passed']: raise RuntimeError('Apply gate failed; no writes performed')
            await self.apply_changes(); await self.validate()
        self.r['counts']=dict(self.r['counts']); self.r['completed_at']=now().isoformat(); return self.r

async def amain(a):
    load_dotenv(a.env_file); url=a.mongo_url or os.getenv('MONGO_URL'); dbn=a.db_name or os.getenv('DB_NAME')
    if not url or not dbn: raise SystemExit('MONGO_URL and DB_NAME are required')
    if a.apply and a.confirm_apply!=CONFIRM: raise SystemExit(f'--apply requires --confirm-apply {CONFIRM}')
    client=AsyncIOMotorClient(url)
    try:
        r=await Runner(client[dbn],a.apply).run(); Path(a.report).write_text(json.dumps(r,indent=2,default=str),encoding='utf-8')
        print(json.dumps({'mode':r['mode'],'report':a.report,'apply_gate_passed':r['apply_gate_passed'],'counts':r['counts'],'bridge':r['bridge'],'ingredients':r['ingredients'],'recipes':r['recipes'],'exception_count':len(r['exceptions'])},indent=2))
    finally: client.close()

def main():
    p=argparse.ArgumentParser(); p.add_argument('--apply',action='store_true'); p.add_argument('--confirm-apply'); p.add_argument('--env-file',default='.env'); p.add_argument('--mongo-url'); p.add_argument('--db-name'); p.add_argument('--report',default='master-canonical-correction-v2-report.json')
    asyncio.run(amain(p.parse_args()))
if __name__=='__main__': main()
