"""Read-only verification of selected B3 development observations; no services."""
import argparse,hashlib,json,re
from datetime import datetime,timedelta
from pathlib import Path

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()
def correlation(job):
    key='b3_'+job['operation_id'].replace('-','') if job['connector_id']=='mock-payments' else job['operation_id']
    return hashlib.sha256(key.encode()).hexdigest()
def date(value):return datetime.fromisoformat(value)
def check(root):
    root=Path(root);summary=json.loads((root/'summary.json').read_text());results=[]
    paths=sorted(root.glob('test_*.json'))
    for path in paths:
        raw=json.loads(path.read_text());name=raw['case']
        assert raw['passed'] and not raw['harness_errors'] and not raw['skipped'],name
        assert all(raw['cleanup'][k] for k in ('schema_dropped','private_files_removed','process_stopped')),name
        # The fixture sends owned-process SIGTERM. This pinned uvicorn build
        # drains and then re-raises that signal (capture_signals), yielding -15.
        # Keep this distinct from a crash or an unobserved leftover process.
        assert raw['cleanup']['process_exit_code']==(-15 if raw['boundary'] else None),name
        assert not re.search(r'sb3s_[A-Za-z0-9_-]{43}',path.read_text()),name
        snapshots=raw['snapshots'];final=snapshots[-1];tables=final['tables']
        assert final['label']=='observation_window_end' and final['source']=='database',name
        assert final['sample_finished_ns']<=raw['observation_window_end_ns'],name
        assert raw['ledger']==raw['ledger_before_cleanup'],name
        assert all(s['application_role'].startswith('safeact_base_') and s['sample_started_ns']<=s['sample_finished_ns'] for s in snapshots),name
        jobs={j['operation_id']:j for j in tables['jobs']};by_key={correlation(j):j for j in jobs.values()}
        effects=[e for e in raw['ledger'] if e['event']=='effect']
        dispatch=[e for e in raw['ledger'] if e['event']=='provider_start' and e.get('method') in ('refund','charge','create_record','send')]
        lookups=[e for e in raw['ledger'] if e['event']=='provider_start' and e.get('method') in ('lookup','search')]
        assert all(e['correlation_sha256'] in by_key for e in effects+dispatch+lookups),name
        for e in raw['ledger']:
            if e['event']=='call':assert e['payload_sha256']==by_key[e['correlation_sha256']]['payload_sha256'],name
        for job in jobs.values():
            op=job['operation_id'];key=correlation(job)
            assert hashlib.sha256(job['payload_canonical'].encode()).hexdigest()==job['payload_sha256'],name
            attempts=sorted((a for a in tables['attempts'] if a['operation_id']==op),key=lambda a:a['ordinal'])
            calls=sorted((e for e in dispatch if e['correlation_sha256']==key),key=lambda e:e['monotonic_ns'])
            own_effects=[e for e in effects if e['correlation_sha256']==key]
            own_obs=[o for o in tables['observations'] if o['operation_id']==op]
            commits=sorted((e for e in raw['timeline'] if e['event']=='after_fence_commit' and e['operation_id']==op),key=lambda e:e['monotonic_ns'])
            assert len(attempts)==len(calls)==len(commits) and len(own_effects)<=1,name
            assert [a['ordinal'] for a in attempts]==list(range(1,len(attempts)+1)),name
            for a,c,event in zip(attempts,commits,calls):
                assert c['monotonic_ns']<event['monotonic_ns'],name
                assert any(r['kind']=='dispatch_fenced' and r['operation_id']==op and r['commit_xid']==c['commit_xid'] for r in tables['audit']),name
                assert any(s['label']=='provider_fence_visible' and s['sample_finished_ns']<event['monotonic_ns'] and
                           any(r['attempt_id']==a['attempt_id'] for r in s['tables']['attempts']) for s in snapshots),name
            for observation in own_obs:
                if observation['kind']=='applied':
                    assert len(own_effects)==1 and observation['reference']==own_effects[0]['effect_id'],name
            if job['status']=='SUCCEEDED':
                assert len(own_effects)==1 and any(o['kind']=='applied' for o in own_obs),name
            elif job['status']=='FAILED_CONFIRMED':
                assert not own_effects and attempts,name
                for attempt in attempts:
                    assert any(o['kind']=='not_applied' and o['reference'] and (o['attempt_id']==attempt['attempt_id'] or
                        o['attempt_id'] is None and o['source']=='lookup' and o['details'].get('through_ordinal',0)>=attempt['ordinal']) for o in own_obs),name
            else:assert job['status']=='ESCALATED_UNRESOLVED',name
        inverse_ids={s['operation_id'] for s in tables['steps'] if s['step']=='compensation'}
        workflow_outcome=None
        if tables['workflows']:
            assert len(tables['workflows'])==1,name
            workflow=tables['workflows'][0];request=workflow['request'];links={s['step']:s for s in tables['steps']}
            charge=jobs[links['charge']['operation_id']]
            assert charge['payload_canonical']==json.dumps(dict(customer_id=request['customer_id'],amount_minor=request['fee_minor']),sort_keys=True,separators=(',',':')),name
            assert charge['resource_id']==request['customer_id'] and charge['provider_account_id']==request['payments_account_id'],name
            assert charge['contract_version']==request['charge_contract'] and charge['action_type']=='charge_shipping_fee',name
            for step,link in links.items():
                j=jobs[link['operation_id']]
                assert link['workflow_id']==workflow['workflow_id'] and j['request_key']==f"wf:{workflow['workflow_id']}:{step}",name
                assert j['tenant_id']==request['tenant_id'] and j['requester_id']==request['requester_id'],name
                admission=[r for r in tables['audit'] if r['operation_id']==j['operation_id'] and r['kind']=='INTENT_RECORDED']
                coordination=[r for r in tables['workflow_audit'] if r['workflow_id']==workflow['workflow_id'] and
                    (r['kind']=='workflow_created' and step=='charge' and r['details']['charge_operation_id']==j['operation_id'] or
                     r['kind']=='step_admitted' and step!='charge' and r['details']['operation_id']==j['operation_id'])]
                assert len(admission)==len(coordination)==1 and admission[0]['commit_xid']==coordination[0]['commit_xid'],name
            order=jobs[links['order']['operation_id']]
            assert order['payload_canonical']==json.dumps(dict(case_id=request['case_id'],sku=request['sku']),sort_keys=True,separators=(',',':')),name
            assert order['resource_id']==request['case_id'] and order['contract_version']==request['order_contract'],name
            assert order['provider_account_id']==request['crm_account_id'] and order['action_type']=='create_replacement_order',name
            assert charge['status']=='SUCCEEDED',name
            if 'compensation' in links:
                inverse=jobs[links['compensation']['operation_id']]
                charge_effect=next(e for e in effects if e['correlation_sha256']==correlation(charge))
                assert order['status']=='FAILED_CONFIRMED' and links['compensation']['compensates_operation_id']==charge['operation_id'],name
                assert inverse['resource_id']==charge_effect['effect_id'] and inverse['provider_account_id']==charge['provider_account_id'],name
                assert inverse['contract_version']==request['compensation_contract'] and inverse['action_type']=='refund_payment',name
                assert json.loads(inverse['payload_canonical'])==dict(charge_id=charge_effect['effect_id'],amount_minor=request['fee_minor']),name
                first_inverse=min(e['monotonic_ns'] for e in dispatch if e['correlation_sha256']==correlation(inverse))
                assert any(s['sample_finished_ns']<first_inverse and
                    any(j['operation_id']==charge['operation_id'] and j['status']=='SUCCEEDED' for j in s['tables']['jobs']) and
                    any(j['operation_id']==order['operation_id'] and j['status']=='FAILED_CONFIRMED' for j in s['tables']['jobs']) and
                    any(l['operation_id']==inverse['operation_id'] for l in s['tables']['steps']) for s in snapshots),name
                workflow_outcome='COMPENSATED_VERIFIED' if inverse['status']=='SUCCEEDED' else 'COMPENSATION_UNRESOLVED'
            else:
                assert order['status'] in ('SUCCEEDED','ESCALATED_UNRESOLVED'),name
                workflow_outcome='COMPLETED' if order['status']=='SUCCEEDED' else 'UNRESOLVED'
            if 'uncertain_order' in name:assert 'compensation' not in links and workflow_outcome=='UNRESOLVED',name
            if 'hidden_refund' in name or 'rejected_refund' in name:assert workflow_outcome=='COMPENSATION_UNRESOLVED',name
            if 'lost_refund' in name or 'actual_order_rejection' in name:assert workflow_outcome=='COMPENSATED_VERIFIED',name
            assert len(links)==len(jobs) and len({s['operation_id'] for s in links.values()})==len(links),name
        proposals={p['proposal_id']:p for p in tables['proposals']}
        actors={a['actor_id']:a for a in tables['humans']}
        for proposal in proposals.values():
            assert any(b['response'].get('proposal_id')==proposal['proposal_id'] and b['response'].get('status')=='proposed'
                       and b['actor_id']==proposal['proposer_id'] and b['response']['binding_sha256']==proposal['binding_sha256'] for b in raw['boundary']),name
        for decision in tables['decisions']:
            proposal=proposals[decision['proposal_id']];job=jobs[proposal['operation_id']]
            assert decision['reviewer_id'] not in (proposal['proposer_id'],job['requester_id']),name
            assert 'reviewer' in actors[decision['reviewer_id']]['roles'] and actors[decision['reviewer_id']]['tenant_id']==job['tenant_id'],name
            assert any(b['status_code']==200 and b['response'].get('status')=='accepted' and
                b['response'].get('proposal_id')==decision['proposal_id'] and b['actor_id']==decision['reviewer_id'] and
                b['request']['decision_key']==decision['decision_key'] and b['request']['approved']==decision['approved'] and
                b['request']['binding_sha256']==proposal['binding_sha256'] for b in raw['boundary']),name
        for grant in tables['authorizations']:
            proposal=proposals[grant['proposal_id']]
            assert grant['basis_sha256']==digest(proposal['basis']['execution']),name
            assert any(d['proposal_id']==grant['proposal_id'] and d['approved'] for d in tables['decisions']),name
            uses=[u for u in tables['authorization_uses'] if u['proposal_id']==grant['proposal_id']]
            assert len(uses)<=1,name
            for use in uses:
                assert date(use['created_at'])<date(grant['expires_at']),name
                if use['attempt_id']:
                    assert grant['action']=='PROTECTED_RETRY' and any(a['attempt_id']==use['attempt_id'] and a['ordinal']==grant['next_ordinal'] for a in tables['attempts']),name
                else:
                    assert grant['action']=='REVERIFY' and any(o['observation_id']==use['lookup_token'] and o['source']=='lookup' for o in tables['observations']),name
        if 'overlapping_reviewers' in name:
            overlap=raw['review_overlap'];graph={int(k):v for k,v in overlap['graph'].items()};holder=overlap['holder_pid']
            def reaches(pid,seen=None):
                seen=set() if seen is None else seen
                if pid==holder:return True
                if pid in seen:return False
                seen.add(pid);return any(reaches(p,seen) for p in graph.get(pid,()))
            assert sum(p!=holder and reaches(p) for p in graph)>=2,name
            first=[b for b in raw['boundary'] if b['request'] and b['request'].get('decision_key')=='key-'+b['actor_id'] and b['response'].get('status') in ('accepted','already_decided')]
            pair=[next(b for b in first if b['actor_id']==a) for a in ('reviewer-one','reviewer-two')]
            assert sorted(b['status_code'] for b in pair)==[200,409],name
            assert max(b['started_ns'] for b in pair)<overlap['observed_at_ns']<min(b['finished_ns'] for b in pair),name
            assert len(tables['decisions'])==len(tables['authorizations'])==len(tables['authorization_uses'])==1,name
            assert sum(b['response'].get('status')=='replayed' for b in raw['boundary'])==2,name
        if 'displayed_proposal_stale' in name:
            stale=next(b for b in raw['boundary'] if b['response'].get('status')=='stale')
            assert stale['status_code']==409 and not tables['decisions'] and not tables['authorizations'],name
            assert any(s['sample_finished_ns']<=stale['started_ns'] and any(j['status']=='SUCCEEDED' for j in s['tables']['jobs'])
                       and any(o['kind']=='applied' for o in s['tables']['observations']) for s in snapshots),name
            assert any(a['kind']=='decision_refused_stale' for a in tables['audit']),name
        if 'past_actual_horizon' in name:
            assert len(tables['authorization_uses'])==2 and all(u['lookup_token'] for u in tables['authorization_uses']),name
            job=next(iter(jobs.values()));assert job['reopens']==1 and len(dispatch)==1,name
            assert min(date(u['created_at']) for u in tables['authorization_uses'])>=date(job['escalated_at'])+timedelta(seconds=20),name
        if 'without_any_broker_delivery' in name:
            assert len(dispatch)==1 and len(tables['attempts'])==1 and not tables['authorization_uses'],name
            assert any(a['kind']=='ready_evidence_changed' for a in tables['audit']),name
        if 'retry_after' in name:
            assert len(dispatch)==2 and not lookups and len(effects)==1,name
            before=next(s['tables']['jobs'][0]['not_before'] for s in snapshots if s['tables']['jobs'] and s['tables']['jobs'][0]['not_before'])
            second=next(a for a in tables['attempts'] if a['ordinal']==2)
            assert date(second['fenced_at'])>=date(before),name
        inverse_effects=sum(e['correlation_sha256']==correlation(jobs[op]) for op in inverse_ids for e in effects)
        results.append(dict(case=name,workflow_outcome=workflow_outcome,jobs=len(jobs),effects=len(effects),
            forward_effects=len(effects)-inverse_effects,inverse_effects=inverse_effects,dispatch_calls=len(dispatch),
            lookup_calls=len(lookups),decisions=len(tables['decisions']),authorization_uses=len(tables['authorization_uses']),
            independent_snapshots=len(snapshots)))
    actual=dict(cases=len(results),passed=len(results),errors=0,cleanup_complete=True,effects=sum(r['effects'] for r in results),
                dispatch_calls=sum(r['dispatch_calls'] for r in results),lookup_calls=sum(r['lookup_calls'] for r in results),external_provider_calls=0,paid_model_calls=0)
    assert {k:summary[k] for k in actual}==actual,(summary,actual)
    return dict(scope='Read-only replay of selected development observations; not the qualification oracle or a baseline comparison',
        summary=actual,forward_effects=sum(r['forward_effects'] for r in results),inverse_effects=sum(r['inverse_effects'] for r in results),cases=results)
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root');p.add_argument('--out');a=p.parse_args();value=check(a.root)
    if a.out:Path(a.out).write_text(json.dumps(value,indent=2)+'\n')
    print(json.dumps(value['summary']));print('forward_effects',value['forward_effects'],'inverse_effects',value['inverse_effects'])
if __name__=='__main__':main()
