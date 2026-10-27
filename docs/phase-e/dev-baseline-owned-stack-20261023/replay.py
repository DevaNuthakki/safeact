"""Check retained registration, publication, process and cleanup joins offline."""

import argparse
import hashlib
import json
from pathlib import Path

from chaos.baselines import registration
from chaos.baselines.store import Request
from chaos.baselines.trace import IDS, validate
from connectors.http_trace import correlation_hash


REFUSALS={
    'test_changed_registered_binding_refuses_a_later_batch',
    'test_controller_death_is_retained_and_never_silently_bypassed',
    'test_failed_mock_registration_leaves_accepted_intent_accounted_without_publication',
    'test_unrequested_clean_exit_cannot_be_hidden_by_a_replacement',
}


def digest(sample):
    fields={k:sample[k] for k in Request.__dataclass_fields__ if k!='payload_canonical'}
    binding=Request.from_payload(payload=sample['payload'],**fields).immutable()
    return hashlib.sha256(json.dumps(binding,sort_keys=True).encode()).hexdigest()


def check_case(value):
    assert value['passed'] and not value['failures'] and not value['skipped'] and all(value['cleanup'].values())
    raw=value['raw'];name=value['case'];negative=name in REFUSALS
    assert raw['declared_integrity_failure']==negative and bool(raw['integrity'])==negative,name
    processes=raw['processes'];starts=[e for e in processes if e['event']=='b3_process_started']
    by_pid={e['pid']:e for e in starts};assert len(by_pid)==len(starts) and by_pid,name
    stopped=[e for e in processes if e['event']=='b3_execution_stopped'];assert len(stopped)==1,name
    drained=[e for e in processes if e['event']=='b3_providers_drained'];assert len(drained)==1,name
    stop=stopped[0];drain=drained[0];assert stop['monotonic_ns']<drain['monotonic_ns'],name
    exits={e['pid']:e['returncode'] for e in stop['processes']};assert set(exits)==set(by_pid),name
    kills=[e for e in processes if e['event']=='b3_process_killed']
    for pid,start in by_pid.items():
        mine=[e for e in kills if e['pid']==pid]
        expected=mine[0]['returncode'] if mine else -15 if start['component']=='api' else 0
        # SIGTERM can reach an already failing relay during interpreter exit.
        # Keep that unexpected exit and error marker in the declared failure case.
        if negative and start['component']=='relay':
            assert exits[pid] in (0,1,-15),name
            if exits[pid]!=0:
                assert f"unexpected_exit:relay:{exits[pid]}" in raw['integrity'],name
                if 'unrequested_clean_exit' not in name:assert 'relay:BASELINE_PROCESS_ERROR' in raw['integrity'],name
        else:assert exits[pid]==expected,name
        assert start['monotonic_ns']<stop['monotonic_ns'],name
        if mine:
            assert len(mine)==1 and mine[0]['signal']==9 and mine[0]['returncode']==-9,name
            requested=[e for e in processes if e['event']=='b3_process_kill_requested' and e['pid']==pid]
            assert len(requested)==1 and requested[0]['signal']==9,name
            assert start['monotonic_ns']<requested[0]['monotonic_ns']<mine[0]['monotonic_ns']<stop['monotonic_ns'],name
    for kind in ('http','execution','broker'):
        seen=set()
        for row in raw[kind]:
            validate(kind,row)
            identity=(row['pid'],row['monotonic_ns'],row['event'],row.get('request_id',row.get('task_id',row.get('point'))))
            assert identity not in seen,name;seen.add(identity)
            assert row['monotonic_ns']<stop['monotonic_ns'],name
    requests={};acks={};finished={}
    for row in raw['registration']:
        suffix={v:k for k,v in registration.SUFFIXES.items()}[row['event']]
        registration.validate(row,row['schema'],suffix)
        assert row['pid'] in by_pid and by_pid[row['pid']]['component']=='relay',name
        table={'request':requests,'ack':acks,'finished':finished}[suffix]
        assert row['request_id'] not in table,name;table[row['request_id']]=row
    assert set(acks)<=set(requests) and set(finished)<=set(acks),name
    samples={e['request_id']:e for e in raw['registration_database']}
    assert len(samples)==len(raw['registration_database']),name
    waits=[]
    for nonce,request in requests.items():
        committed=[e for e in raw['execution'] if e['event']=='commit_returned' and e.get('stage')=='publication'
            and e['pid']==request['pid'] and e['commit_xid']==request['commit_xid']]
        assert len(committed)==1 and committed[0]['monotonic_ns']<request['monotonic_ns'],name
        before=[e for e in raw['execution'] if e['event']=='commit_before' and e.get('stage')=='publication'
            and e['pid']==request['pid'] and e['commit_xid']==request['commit_xid']]
        assert len(before)==1 and before[0]['monotonic_ns']<committed[0]['monotonic_ns'],name
        assert not any(e['event']=='hook_error' and e.get('stage')=='publication' and e['pid']==request['pid']
            and e['commit_xid']==request['commit_xid'] for e in raw['execution']),name
        if nonce not in acks:continue
        ack=acks[nonce]
        assert all(ack[k]==request[k] for k in ('schema','pid','commit_xid','request_id')),name
        assert request['monotonic_ns']<=ack['monotonic_ns'],name
        if ack['status']=='refused':assert negative and nonce not in finished,name;continue
        assert nonce in samples,name;sample=samples[nonce]
        assert sample['event']=='registration_database_sample' and sample['source']=='database',name
        assert all(sample[k]==request[k] for k in ('schema','pid','commit_xid','request_id')),name
        assert request['monotonic_ns']<=sample['sample_started_ns']<=sample['sample_finished_ns']<=sample['registration_finished_ns']<=ack['monotonic_ns'],name
        for binding in sample['bindings']:
            assert binding['connector_id']=='mock-payments',name
            assert binding['correlation_sha256']==correlation_hash('b3_'+binding['operation_id'].replace('-','')),name
            matching=[e for e in raw['database'] if e['event']=='db_operation_sample'
                and all(e[k]==binding[k] for k in ('operation_id','connector_id','contract_version','correlation_sha256'))]
            assert any(digest(e)==binding['binding_sha256'] for e in matching),name
        if nonce in finished:
            end=finished[nonce]
            assert all(end[k]==ack[k] for k in ('schema','pid','commit_xid','request_id','status')),name
            assert ack['monotonic_ns']<=end['monotonic_ns']<stop['monotonic_ns'],name
            waits.append(end['monotonic_ns']-request['monotonic_ns'])
    published=[e for e in raw['broker'] if e['event']=='publish_started']
    for publish in published:
        preceding=[e for e in raw['execution'] if e['event']=='commit_returned' and e.get('stage')=='publication'
            and e['pid']==publish['pid'] and e['monotonic_ns']<publish['monotonic_ns']]
        assert preceding,name;commit=max(preceding,key=lambda e:e['monotonic_ns'])
        gates=[e for e in finished.values() if e['pid']==publish['pid'] and e['commit_xid']==commit['commit_xid']]
        assert len(gates)==1 and gates[0]['monotonic_ns']<publish['monotonic_ns'],name
        assert any(all(str(d[k])==publish[k] for k in IDS[:3]) for d in raw['committed_deliveries']),name
        native=[e for e in raw['database'] if e['event']=='db_operation_sample' and e['operation_id']==publish['operation_id']
            and e['connector_id']=='mock-payments']
        if native:assert any(b['operation_id']==publish['operation_id'] for b in samples[gates[0]['request_id']]['bindings']),name
    effects=[e for e in raw['ledger'] if e['event']=='effect']
    assert len({e['correlation_sha256'] for e in effects})==len(effects),name
    for effect in effects:
        assert effect['monotonic_ns']<drain['monotonic_ns'],name
        sends=[e for e in raw['http'] if e['event']=='http_start' and e['method']=='POST'
            and e['correlation_sha256']==effect['correlation_sha256']]
        assert len(sends)==1 and sends[0]['monotonic_ns']<effect['monotonic_ns'],name
    if any(s in name for s in ('controller_death','failed_mock_registration','shutdown_cancels')):
        assert requests and not published and not raw['ledger'],name
        assert set(v['state'] for v in raw['final_operations'].values())=={'RECORDED'},name
    if 'shutdown_cancels' in name:assert not finished,name
    if 'changed_registered_binding' in name:
        assert len(effects)==1 and {'RECORDED','SUCCEEDED'}==set(v['state'] for v in raw['final_operations'].values()),name
    if 'unrequested_clean_exit' in name:
        assert 'unexpected_exit_before_restart:relay:0' in raw['integrity'],name
        assert len(starts)==2 and not effects and not raw['ledger'],name
        assert set(v['state'] for v in raw['final_operations'].values())=={'RECORDED'},name
    if 'restart' in name:
        component='relay' if 'relay_restart' in name else 'worker'
        assert len(kills)==1 and by_pid[kills[0]['pid']]['component']==component,name
        assert len([e for e in starts if e['component']==component])==2,name
        if component=='relay':
            assert {e['pid'] for e in requests.values()}=={e['pid'] for e in starts if e['component']=='relay'},name
            assert all(e['pid']!=kills[0]['pid'] for e in finished.values()),name
    if 'lost_publication_ack' in name:
        errors=[e for e in raw['execution'] if e['event']=='hook_error' and e.get('stage')=='publication']
        assert len(errors)==1 and errors[0]['error_type']=='LostAcknowledgement',name
        assert all(e['commit_xid']!=errors[0]['commit_xid'] for e in requests.values()),name
        assert len(raw['committed_deliveries'])>=2 and len(effects)==1,name
    if 'held_late_effect' in name:
        assert len(effects)==1 and stop['monotonic_ns']<effects[0]['monotonic_ns'],name
        assert set(v['state'] for v in raw['final_operations'].values())=={'UNKNOWN'},name
    if 'workflow_advancer' in name:
        assert len(effects)==2 and set(v['state'] for v in raw['final_operations'].values())=={'COMPENSATED_VERIFIED'},name
        versions={b['contract_version'] for e in samples.values() for b in e['bindings']}
        assert {'q-v2-eventual','q-v2-short'}<=versions,name
    return dict(case=name,expected_integrity_refusal=negative,process_generations=len(starts),publications=len(published),
        completed_gates=len(finished),effects=len(effects),gate_wait_ns=waits)


def check(root):
    root=Path(root);rows=[check_case(json.loads(p.read_text())) for p in sorted((root/'raw').glob('test_*.json'))]
    summary=json.loads((root/'raw/summary.json').read_text())
    assert len(rows)==summary['cases']==summary['passed']==13 and summary['cleanup_complete']
    assert summary['effects']==sum(r['effects'] for r in rows)
    assert summary['expected_integrity_refusals']==sum(r['expected_integrity_refusal'] for r in rows)==4
    return dict(scope='Offline development evidence joins; not qualification coverage, scoring or latency comparison',cases=rows)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root');p.add_argument('--out');a=p.parse_args();value=check(a.root)
    if a.out:Path(a.out).write_text(json.dumps(value,indent=2)+'\n')
    print('Replayed',len(value['cases']),'owned-stack development cases')
