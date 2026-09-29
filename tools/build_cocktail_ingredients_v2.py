#!/usr/bin/env python3
"""Build DrinkThink canonical cocktail_ingredients from the curated legacy recipe resolution.

Safe default: dry-run. Apply requires --apply --confirm-apply BUILD_COCKTAIL_INGREDIENTS_V2.
Legacy numeric recipe ingredient IDs are translated through the original ingredient catalog
into canonical semantic string IDs. Only fully-resolved recipes are written.
"""
import argparse, ast, asyncio, csv, json, os, re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

VERSION='cocktail-ingredients-v2'
CONFIRM='BUILD_COCKTAIL_INGREDIENTS_V2'
SPIRIT_PRIMARY_NAMES={'vodka','gin','rum','whiskey','tequila'}

def now(): return datetime.now(timezone.utc)
def norm(v): return re.sub(r'\s+',' ',re.sub(r'[^a-z0-9]+',' ',str(v or '').lower())).strip()
def parse_ids(v):
    if v is None: return []
    if isinstance(v,list): return [str(x).strip() for x in v if str(x).strip()]
    s=str(v).strip()
    if not s or s.lower()=='nan': return []
    try:
        x=ast.literal_eval(s)
        if isinstance(x,list): return [str(i).strip() for i in x if str(i).strip()]
    except Exception: pass
    return [x.strip() for x in re.split(r'[,|]',s) if x.strip()]

def clean_unmatched(v):
    if v is None: return ''
    s=str(v).strip()
    return '' if not s or s.lower() in {'nan','none'} else s

class Runner:
    def __init__(self,db,csv_path,apply):
        self.db=db; self.csv_path=csv_path; self.apply=apply
        self.report={'version':VERSION,'mode':'APPLY' if apply else 'DRY_RUN','started_at':now().isoformat(),
                     'counts':Counter(),'exceptions':[],'validation':{}}

    def build_legacy_ingredient_bridge(self):
        catalog=Path(__file__).resolve().parents[1]/'data'/'legacy_ingredient_catalog.csv'
        if not catalog.exists(): raise RuntimeError(f'Legacy ingredient catalog not found: {catalog}')
        by_parent_name=defaultdict(list); by_name=defaultdict(list)
        for iid,d in self.ingredients.items():
            if d.get('ingredient_type') != 'item': continue
            n=norm(d.get('name')); pid=str(d.get('parent_ingredient_id') or '')
            by_parent_name[(pid,n)].append(iid); by_name[n].append(iid)
        bridge={}; ambiguous=[]; missing=[]
        with catalog.open('r',encoding='utf-8-sig',newline='') as f:
            for r in csv.DictReader(f):
                old=str(r.get('ID','')).strip(); name=norm(r.get('All ingredients')); parent=norm(r.get('Primary Ingredient'))
                candidates=by_parent_name.get((parent,name),[])
                if len(candidates)!=1: candidates=by_name.get(name,[])
                if len(candidates)==1: bridge[old]=candidates[0]
                elif len(candidates)>1: ambiguous.append({'legacy_ingredient_id':old,'name':r.get('All ingredients'),'candidates':candidates})
                else: missing.append({'legacy_ingredient_id':old,'name':r.get('All ingredients'),'primary':r.get('Primary Ingredient')})
        self.report['counts']['legacy_ingredient_catalog_rows']=len(bridge)+len(ambiguous)+len(missing)
        self.report['counts']['legacy_ingredient_ids_mapped']=len(bridge)
        self.report['counts']['legacy_ingredient_ids_ambiguous']=len(ambiguous)
        self.report['counts']['legacy_ingredient_ids_unmapped']=len(missing)
        self.report['ingredient_bridge_exceptions']={'ambiguous':ambiguous[:100],'unmapped':missing[:100]}
        return bridge

    async def load(self):
        self.ingredients={d['ingredient_id']:d async for d in self.db.ingredients.find({}, {'_id':0})}
        self.legacy_ingredient_bridge=self.build_legacy_ingredient_bridge()
        self.cocktails=[d async for d in self.db.cocktails.find({'status':'active'},{'_id':0})]
        self.legacy={str(d['_id']):d async for d in self.db.drinks.find({})}
        self.report['counts']['canonical_cocktails']=len(self.cocktails)
        self.report['counts']['canonical_ingredients']=len(self.ingredients)
        self.report['counts']['legacy_drinks']=len(self.legacy)
        self.csv_rows={}
        if self.csv_path and self.csv_path.exists():
            with self.csv_path.open('r',encoding='utf-8-sig',newline='') as f:
                for r in csv.DictReader(f):
                    key=(str(r.get('id','')).strip(),norm(r.get('d_name')))
                    self.csv_rows[key]=r
            self.report['counts']['curated_csv_rows']=len(self.csv_rows)

    def resolution_for(self,c):
        mig=c.get('migration') or {}
        source_id=str(mig.get('source_mongo_id') or '')
        legacy_id=str(mig.get('legacy_drink_id') or '').strip()
        old=self.legacy.get(source_id,{})
        # Prefer resolution fields already stored on the exact legacy Mongo source document.
        ids=parse_ids(old.get('ingredient_ids'))
        unmatched=clean_unmatched(old.get('ingredient_unmatched'))
        source='legacy_document'
        if not ids and self.csv_rows:
            row=self.csv_rows.get((legacy_id,norm(c.get('name'))))
            if row:
                ids=parse_ids(row.get('ingredient_ids'))
                unmatched=clean_unmatched(row.get('ingredient_unmatched'))
                pct=str(row.get('match_pct_recheck') or '').strip()
                if pct and pct.lower()!='nan':
                    try:
                        if float(pct) < 100: unmatched=unmatched or f'match_pct_recheck={pct}'
                    except ValueError: pass
                source='curated_csv'
        translated=[]; missing=[]
        for old_id in ids:
            iid=self.legacy_ingredient_bridge.get(str(old_id))
            if iid and iid in self.ingredients: translated.append(iid)
            else: missing.append(str(old_id))
        complete=bool(translated) and not unmatched and not missing and len(translated)==len(ids)
        return complete,translated,missing,unmatched,source

    def primary_id(self,iid):
        d=self.ingredients[iid]
        return str(d.get('parent_ingredient_id') or iid)

    async def build(self):
        rows=[]; cocktail_updates=[]; unresolved=[]
        for c in self.cocktails:
            complete,ids,missing,unmatched,source=self.resolution_for(c)
            cid=c['cocktail_id']
            if not complete:
                unresolved.append({'cocktail_id':cid,'name':c.get('name'),'legacy_drink_id':(c.get('migration') or {}).get('legacy_drink_id'),
                                   'reason':unmatched or ('missing canonical ingredient ids: '+','.join(missing) if missing else 'no complete curated ingredient resolution')})
                continue
            seen=set(); ordered=[]
            for iid in ids:
                if iid not in seen: seen.add(iid); ordered.append(iid)
            primary=[]
            for iid in ordered:
                pid=self.primary_id(iid)
                pd=self.ingredients.get(pid,{})
                if norm(pd.get('name')) in SPIRIT_PRIMARY_NAMES and pid not in primary: primary.append(pid)
            for pos,iid in enumerate(ordered):
                rows.append({'cocktail_id':cid,'ingredient_id':iid,'position':pos,'required':True,
                             'resolution_source':source,'status':'active','created_at':now(),'updated_at':now()})
            cocktail_updates.append((cid,primary,len(ordered)))
        self.rows=rows; self.updates=cocktail_updates; self.unresolved=unresolved
        self.report['counts'].update({'fully_resolved_cocktails':len(cocktail_updates),'unresolved_cocktails':len(unresolved),
                                      'cocktail_ingredient_rows':len(rows)})
        self.report['exceptions']=unresolved[:250]
        self.report['exceptions_truncated']=len(unresolved)>250

    async def write(self):
        if not self.apply:return
        await self.db.cocktail_ingredients.delete_many({})
        if self.rows: await self.db.cocktail_ingredients.insert_many(self.rows,ordered=False)
        # Clear then set derived canonical main ingredients only for fully-resolved recipes.
        await self.db.cocktails.update_many({}, {'$set':{'main_ingredient_ids':[],'recipe_resolution.status':'unresolved','recipe_resolution.version':VERSION}})
        for cid,primary,count in self.updates:
            await self.db.cocktails.update_one({'cocktail_id':cid},{'$set':{
                'main_ingredient_ids':primary,
                'recipe_resolution':{'status':'resolved','version':VERSION,'ingredient_count':count,'updated_at':now()}
            }})
        await self.db.cocktail_ingredients.create_index([('cocktail_id',1),('ingredient_id',1)],unique=True)
        await self.db.cocktail_ingredients.create_index('ingredient_id')
        await self.db.cocktails.create_index('main_ingredient_ids')

    async def validate(self):
        self.report['validation']={
            'all_written_ingredient_ids_exist': all(r['ingredient_id'] in self.ingredients for r in self.rows),
            'duplicate_cocktail_ingredient_pairs': len(self.rows)-len({(r['cocktail_id'],r['ingredient_id']) for r in self.rows}),
            'resolved_plus_unresolved_equals_canonical': len(self.updates)+len(self.unresolved)==len(self.cocktails),
            'coverage_pct': round((len(self.updates)/len(self.cocktails)*100),2) if self.cocktails else 0,
        }
        if self.apply:
            self.report['validation']['db_cocktail_ingredient_rows']=await self.db.cocktail_ingredients.count_documents({})
            self.report['validation']['db_resolved_cocktails']=await self.db.cocktails.count_documents({'recipe_resolution.status':'resolved'})

    async def run(self):
        await self.load(); await self.build(); await self.write(); await self.validate()
        self.report['counts']=dict(self.report['counts']); self.report['completed_at']=now().isoformat()
        return self.report

async def main(a):
    root=Path(__file__).resolve().parents[1]; load_dotenv(root/'.env')
    if a.apply and a.confirm_apply!=CONFIRM: raise SystemExit(f'Apply refused. Pass --confirm-apply {CONFIRM}')
    url=os.environ['MONGO_URL']; dbn=os.environ['DB_NAME']; client=AsyncIOMotorClient(url); db=client[dbn]
    try:
        r=await Runner(db,Path(a.curated_csv) if a.curated_csv else None,a.apply).run()
        Path(a.report).write_text(json.dumps(r,indent=2,default=str),encoding='utf-8')
        print(json.dumps({'mode':r['mode'],'counts':r['counts'],'validation':r['validation'],'report':a.report},indent=2))
    finally: client.close()

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--apply',action='store_true'); p.add_argument('--confirm-apply',default='')
    p.add_argument('--curated-csv',default=str(Path(__file__).resolve().parents[1]/'data'/'drinks_clean_100pct.csv'))
    p.add_argument('--report',default='cocktail-ingredients-report-v2.json')
    asyncio.run(main(p.parse_args()))
