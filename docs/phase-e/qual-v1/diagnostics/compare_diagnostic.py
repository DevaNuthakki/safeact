import json, sys, collections
F='docs/phase-e/qual-v1/full'; D='docs/phase-e/qual-v1/diagnostics'
def rows(p): return [json.loads(l) for l in open(p)]
prim={(r['scenario_id'],r['arm']):r for r in rows(F+'/results.jsonl')}
diag={(r['scenario_id'],r['arm']):r for r in rows(D+'/results.jsonl')}
flag={(x['scenario_id'],x['arm']):x['reasons'] for x in json.load(open(F+'/load-suspect.json'))}
def safety(r): return bool(r.get('prohibited_duplicates',0) or r.get('orphan_effects',0) or r.get('unaccounted',0) or r.get('changed_accepted',0) or r.get('false_claims') or r.get('integrity',[]))
def useful(r):
    ops=r.get('operations') or []
    return sum(o['safe_completion'] for o in ops), (sum(1 for o in ops) if r.get('completion_achievable') else 0)
def sig(r): return dict(dup=r.get('prohibited_duplicates',0),orph=r.get('orphan_effects',0),unacc=r.get('unaccounted',0),chg=r.get('changed_accepted',0),fc=r.get('false_claims',0),
    outcomes=sorted(o['outcome_class'] for o in r.get('operations') or []),effects=sum(o['effects'] for o in r.get('operations') or []),err=bool(r.get('harness_errors',[])),na=r.get('not_applicable',False))
out=[];cnt=collections.Counter()
for k in sorted(diag):
    p,d=prim[k],diag[k]; ps,ds=sig(p),sig(d)
    cat=('flagged' if k in flag else 'companion')
    c={'scenario_id':k[0],'arm':k[1],'role':cat,'load_suspect_reasons':flag.get(k,[]),'primary':ps,'diagnostic':ds,
       'primary_safety_failure':safety(p),'diagnostic_safety_failure':safety(d),'same_outcome_fields':ps==ds}
    out.append(c); cnt[(cat,k[1],safety(p),safety(d))]+=1
for k,v in sorted(cnt.items()): print(k,v)
print('flagged',len(flag),'covered',sum(1 for k in flag if k in diag))
diff=[c for c in out if not c['same_outcome_fields']]
print('differ',len(diff)); 
for c in diff[:400]:
    print(c['scenario_id'],c['arm'],c['role'],'P',c['primary'],'D',c['diagnostic'])
json.dump(out,open(sys.argv[1],'w'),indent=1)
