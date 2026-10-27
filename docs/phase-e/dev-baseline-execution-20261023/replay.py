"""Independently check retained B3 development observations; no execution."""
import argparse,json
from pathlib import Path
from datetime import datetime

def check(root):
    root=Path(root)
    summary=json.loads((root/'summary.json').read_text())
    cases=sorted(p for p in root.glob('*.json') if p.name not in ('summary.json','replay.json'))
    rows=[]
    for path in cases:
        raw=json.loads(path.read_text());name=raw['case']
        assert not raw['harness_errors'],name
        assert raw['cleanup_complete'],name
        final=raw['snapshots'][-1];tables=final['tables'];job=tables['jobs'][0]
        assert final['label']=='after_execution_and_provider_shutdown',name
        assert final['source']=='database' and final['application_role'].startswith('safeact_base_'),name
        assert len(tables['jobs'])==1 and job['operation_id']==raw['operation_id'],name
        assert job['status']==raw['outcome']==raw['expected_outcome'],name
        attempts=sorted(tables['attempts'],key=lambda a:a['ordinal'])
        observations=tables['observations'];audit=tables['audit']
        assert [a['ordinal'] for a in attempts]==list(range(1,len(attempts)+1)),name
        ledger=raw['ledger'];key=raw['correlation_sha256']
        effects=[e for e in ledger if e['event']=='effect']
        starts=[e for e in ledger if e['event']=='provider_start' and e.get('method') in ('refund','charge','create_record','send')]
        lookups=[e for e in ledger if e['event']=='provider_end' and e.get('method') in ('lookup','search')]
        assert len(effects)==1 and all(e['correlation_sha256']==key for e in effects+starts),name
        assert len(starts)==len(attempts),name
        traces=raw['timeline']+[e for p in raw['processes'] for e in p['trace']]
        commits=[e for e in traces if (e.get('event')=='after_fence_commit' or
            e.get('event')=='hook' and e.get('point')=='after_fence_commit')]
        commits.sort(key=lambda e:e['monotonic_ns']);starts.sort(key=lambda e:e['monotonic_ns'])
        assert len(commits)==len(attempts),name
        for a,c,s in zip(attempts,commits,starts):
            assert c['monotonic_ns']<s['monotonic_ns'],name
            assert datetime.fromisoformat(a['fenced_at']).timestamp()<=s['at'],name
            assert any(e['kind']=='dispatch_fenced' and e['commit_xid']==c['commit_xid'] for e in audit),name
        for a,b in zip(commits,starts[1:]):assert a['monotonic_ns']<b['monotonic_ns'],name
        assert len(tables['deliveries'])>=1 and len(tables['outbox'])>=1,name
        for o in observations:
            assert o['operation_id']==raw['operation_id'],name
            if o['kind']=='applied':assert o['reference']==effects[0]['effect_id'],name
        if job['status']=='SUCCEEDED':assert any(o['kind']=='applied' for o in observations),name
        else:assert job['status']=='ESCALATED_UNRESOLVED',name
        http=raw['http']
        http_starts={e['request_id']:e for e in http if e['event']=='http_start'}
        for e in http:
            if e['event']!='http_start':
                assert e['request_id'] in http_starts and e['monotonic_ns']>=http_starts[e['request_id']]['monotonic_ns'],name
        if name=='worker-crash-after-send':
            assert len(raw['processes'])==1 and raw['processes'][0]['returncode']==-9,name
            p=raw['processes'][0];assert p['marker']['pid']==p['pid'],name
            killed=[e for e in raw['timeline'] if e['event']=='worker_kill_requested'][0]
            assert effects[0]['monotonic_ns']<killed['monotonic_ns'],name
            assert not any(o['source']=='dispatch' for o in observations),name
            assert any(o['source']=='lookup' and o['kind']=='applied' for o in observations),name
        if name=='late-crm-after-empty':
            empty=[e for e in lookups if e.get('lookup_status')=='absent']
            assert empty and min(e['monotonic_ns'] for e in empty)<effects[0]['monotonic_ns'],name
            received=[e for e in http if e['event']=='http_response' and e['method']=='GET' and e['status_code']==200
                      and e['monotonic_ns']<effects[0]['monotonic_ns']]
            assert received and len(starts)==1,name
        if name=='lookup-past-key-window':
            expiry=[e for e in ledger if e['event']=='provider_key_expired']
            assert expiry and expiry[0]['age_seconds']>=6,name
            assert not any(o['kind']=='not_applied' for o in observations),name
            assert any(o['source']=='lookup' and o['details'].get('note')=='lookup_finished_after_key_window' for o in observations),name
            assert len(starts)==1,name
        if name=='native-replay':assert len(starts)==2 and len(effects)==1,name
        if name=='ambiguous-email':assert len(starts)==1 and not lookups,name
        rows.append(dict(case=name,state=job['status'],effects=len(effects),dispatch_calls=len(starts),
                         lookup_calls=sum(e['event']=='provider_start' and e.get('method') in ('lookup','search') for e in ledger),
                         committed_fences=len(attempts),observations=len(observations),independent_snapshots=len(raw['snapshots'])))
    actual=dict(cases=len(rows),errors=0,achievable=sum(r['state']=='SUCCEEDED' for r in rows),
                completed=sum(r['state']=='SUCCEEDED' for r in rows),unresolved=sum(r['state']=='ESCALATED_UNRESOLVED' for r in rows),
                effects=sum(r['effects'] for r in rows),dispatch_calls=sum(r['dispatch_calls'] for r in rows),
                all_cleanup_complete=True,external_provider_calls=0,paid_model_calls=0)
    assert {k:summary[k] for k in actual}==actual,(summary,actual)
    return dict(scope='Read-only replay of local development observations; not the qualification oracle or a baseline comparison',
                summary=actual,cases=rows,checks=['independent final snapshots','committed fence before each provider dispatch',
                'matching independent effects and actual positive references','late commit after empty HTTP answer',
                'actual native-key expiry without false non-application','worker SIGKILL after effect and lookup recovery'])
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root');p.add_argument('--out');a=p.parse_args()
    value=check(a.root)
    if a.out:Path(a.out).write_text(json.dumps(value,indent=2)+'\n')
    print(json.dumps(value['summary']))
if __name__=='__main__':main()
