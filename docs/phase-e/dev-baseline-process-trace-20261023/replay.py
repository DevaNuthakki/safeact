"""Validate saved B3 process traces and independent joins without services."""

import argparse
import hashlib
import json
from pathlib import Path

from chaos.baselines.trace import IDS, validate
from chaos.coverage import observed
from connectors.http_trace import correlation_hash


def check_case(value):
    assert value['passed'] and not value['failures'] and not value['skipped'] and all(value['cleanup'].values())
    raw=value['raw'];name=value['case']
    assert not raw['forbidden_trace_markers']
    for process in raw['processes']:
        assert process['returncode']==(-9 if process['component']=='execute' else 0),name
    for kind in ('http','execution','broker'):
        seen=set()
        for row in raw[kind]:
            validate(kind,row)
            key=(row['pid'],row['monotonic_ns'],row['event'],row.get('request_id',row.get('task_id',row.get('point'))))
            assert key not in seen,name;seen.add(key)
    ledger=raw['ledger'];execution=raw['execution'];http=raw['http'];joined=set()
    for event in http:
        mine=[e for e in http if e['request_id']==event['request_id']]
        assert len(mine)==2 and {e['event'] for e in mine} in (
            {'http_start','http_response'},{'http_start','http_error'}),name
    methods={'/v1/refunds':'refund','/v1/charges':'charge','/records':'create_record','/messages':'send'}
    for start in (e for e in http if e['event']=='http_start'):
        endings=[e for e in http if e['event'] in ('http_response','http_error') and e['request_id']==start['request_id']]
        assert len(endings)==1,name
        end=endings[0];key=start['correlation_sha256']
        assert all(start[k]==end[k] for k in ('provider','method','path','correlation_sha256','pid')),name
        method=methods[start['path']] if start['method']=='POST' else 'lookup' if start['provider']=='payments' else 'search'
        providers=[e for e in ledger if e['event']=='provider_start' and e.get('correlation_sha256')==key
            and e.get('provider')==start['provider'] and e.get('method')==method
            and start['monotonic_ns']<=e['monotonic_ns']<=end['monotonic_ns']]
        assert len(providers)==1,name
        provider=providers[0]
        assert provider['provider_call_id'] not in joined,name
        joined.add(provider['provider_call_id'])
        endings=[e for e in ledger if e['event'] in ('provider_end','provider_error')
                 and e.get('provider_call_id')==provider['provider_call_id']]
        assert len(endings)==1 and provider['monotonic_ns']<=endings[0]['monotonic_ns']<=end['monotonic_ns'],name
        if start['method']=='POST':
            fences=[e for e in execution if e['event']=='commit_returned' and e.get('stage')=='fence'
                and e['pid']==start['pid'] and e['monotonic_ns']<start['monotonic_ns']
                and correlation_hash('b3_'+e['operation_id'].replace('-','') if start['provider']=='payments'
                                     else e['operation_id'])==key]
            assert len(fences)==1,name
            if any(a['local_id']==fences[0]['operation_id'] for a in raw['admissions']):
                assert any(e['event']=='db_audit_committed' and e['commit_xid']==fences[0]['commit_xid']
                           and e['operation_id']==fences[0]['operation_id'] for e in raw['database']),name
    assert joined=={e['provider_call_id'] for e in ledger if e['event']=='provider_start'},name
    actual=sorted(observed(raw));assert actual==raw['observed_coverage'],name
    if 'lost_' in name and '_ack_' in name:assert 'commit_ack_lost' in actual,name
    if 'lost_fence_ack' in name:assert not http and not ledger,name
    if 'sigkill' in name:
        assert 'worker_crashed_after_send' in actual,name
        assert len([e for e in ledger if e['event']=='effect'])==1,name
        assert len([e for e in http if e['event']=='http_start' and e['method']=='POST'])==1,name
    if 'prefork' in name:
        starts=[e for e in raw['broker'] if e['event']=='task_started']
        completed=[e for e in raw['broker'] if e['event']=='task_returned' and e.get('task_status')=='recorded']
        assert len(completed)==1,name
        end=completed[0];matching=[e for e in starts if e['task_id']==end['task_id']]
        assert len(matching)==1,name
        start=matching[0]
        received=[e for e in raw['broker'] if e['event']=='task_received' and all(e[k]==start[k] for k in IDS)]
        publications=[e for e in raw['broker'] if e['event']=='publish_returned' and all(e[k]==start[k] for k in IDS)]
        assert len(received)==len(publications)==1,name
        assert publications[0]['monotonic_ns']<received[0]['monotonic_ns']<start['monotonic_ns']<end['monotonic_ns'],name
        assert start['pid']==end['pid'] and received[0]['pid']!=start['pid'],name
        assert any(all(d[k]==start[k] for k in IDS[:3]) for d in raw['committed_deliveries']),name
        assert any(e['event']=='http_start' and e['method']=='POST' and e['pid']==start['pid'] for e in http),name
    if 'workflow_commit' in name:
        assert len(raw['workflow_audit'])==2,name
        for audit in raw['workflow_audit']:
            assert any(e['event']=='commit_returned' and e.get('workflow_id')==audit['workflow_id']
                       and e['commit_xid']==audit['commit_xid'] for e in execution),name
    effects=[e for e in ledger if e['event']=='effect']
    assert len({e['correlation_sha256'] for e in effects})==len(effects),name
    return dict(case=name,effects=len(effects),http_dispatches=sum(e['event']=='http_start' and e['method']=='POST' for e in http),
        http_lookups=sum(e['event']=='http_start' and e['method']=='GET' for e in http),observed_interleavings=actual)


def check(root):
    root=Path(root);rows=[check_case(json.loads(p.read_text())) for p in sorted((root/'raw').glob('test_*.json'))]
    assert len(rows)==7
    summary=json.loads((root/'raw/summary.json').read_text())
    assert summary['cases']==summary['passed']==len(rows) and summary['cleanup_complete']
    for key in ('effects','http_dispatches','http_lookups'):assert summary[key]==sum(r[key] for r in rows)
    return dict(scope='Read-only replay of selected process observation checks; not frozen family coverage or comparison',cases=rows)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root');p.add_argument('--out');a=p.parse_args();value=check(a.root)
    if a.out:Path(a.out).write_text(json.dumps(value,indent=2)+'\n')
    print('Replayed',len(value['cases']),'actual process cases')
