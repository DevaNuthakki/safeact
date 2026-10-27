"""Synthetic retained-data counterexamples for the development verifier only."""
import argparse,copy,json,shutil,tempfile
from pathlib import Path
from replay import check

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root');p.add_argument('--out',required=True);a=p.parse_args()
    source=Path(a.root);check(source);results=[]
    def fixture(fragment):return next(p for p in source.glob('test_*.json') if fragment in p.name)
    def inverse(raw):return next(j for j in raw['snapshots'][-1]['tables']['jobs'] if j['action_type']=='refund_payment')
    def wrong_amount(raw):
        # Keep the forged DB/call payload hashes internally consistent so
        # rejection must come from the authorized workflow amount binding.
        import hashlib
        from replay import correlation
        job=inverse(raw);op=job['operation_id'];key=correlation(job)
        payload=json.dumps(dict(amount_minor=501,charge_id=job['resource_id']),sort_keys=True,separators=(',',':'))
        hashed=hashlib.sha256(payload.encode()).hexdigest()
        for snapshot in raw['snapshots']:
            for row in snapshot['tables']['jobs']:
                if row['operation_id']==op:row.update(payload_canonical=payload,payload_sha256=hashed)
        for events in (raw['ledger'],raw['ledger_before_cleanup']):
            for event in events:
                if event['event']=='call' and event['correlation_sha256']==key:event['payload_sha256']=hashed
    def wrong_link(raw):
        steps=raw['snapshots'][-1]['tables']['steps'];next(s for s in steps if s['step']=='compensation')['compensates_operation_id']=next(s for s in steps if s['step']=='order')['operation_id']
    def duplicate(raw):
        key=next(j for j in raw['snapshots'][-1]['tables']['steps'] if j['step']=='compensation')['operation_id']
        from replay import correlation
        corr=correlation(next(j for j in raw['snapshots'][-1]['tables']['jobs'] if j['operation_id']==key))
        effect=copy.deepcopy(next(e for e in raw['ledger'] if e['event']=='effect' and e['correlation_sha256']==corr));effect['effect_id']='duplicate-effect'
        raw['ledger'].append(effect);raw['ledger_before_cleanup'].append(copy.deepcopy(effect))
    def false_success(raw):inverse(raw)['status']='SUCCEEDED'
    def orphan(raw):
        effect=dict(event='effect',correlation_sha256='f'*64,effect_id='orphan',monotonic_ns=0)
        raw['ledger'].append(effect);raw['ledger_before_cleanup'].append(copy.deepcopy(effect))
    def cleanup_only(raw):raw['ledger'].append(dict(event='effect',correlation_sha256='f'*64,effect_id='cleanup-only'))
    def self_approve(raw):
        t=raw['snapshots'][-1]['tables'];t['decisions'][0]['reviewer_id']=t['jobs'][0]['requester_id']
    def missing_role(raw):
        t=raw['snapshots'][-1]['tables'];winner=t['decisions'][0]['reviewer_id']
        next(h for h in t['humans'] if h['actor_id']==winner)['roles']=[]
    def fake_overlap(raw):raw['review_overlap']['graph']={}
    def duplicate_decision(raw):raw['snapshots'][-1]['tables']['decisions'].append(copy.deepcopy(raw['snapshots'][-1]['tables']['decisions'][0]))
    def missing_fence(raw):
        next(e for e in raw['timeline'] if e['event']=='after_fence_commit')['commit_xid']='uncommitted-fence'
    def stale_without_committed_evidence(raw):
        for s in raw['snapshots']:
            if s['label']!='observation_window_end':s['tables']['observations']=[]
    def extra_authorization_use(raw):
        t=raw['snapshots'][-1]['tables'];t['authorization_uses'].append(copy.deepcopy(t['authorization_uses'][0]))
    def early_retry(raw):
        attempts=raw['snapshots'][-1]['tables']['attempts'];first=next(x for x in attempts if x['ordinal']==1)
        next(x for x in attempts if x['ordinal']==2)['fenced_at']=first['fenced_at']
    def no_ready_recovery(raw):
        for e in raw['snapshots'][-1]['tables']['audit']:
            if e['kind']=='ready_evidence_changed':e['kind']='unrelated-event'
    cases=(('wrong-inverse-amount','actual_order_rejection',wrong_amount),('wrong-original-link','actual_order_rejection',wrong_link),
      ('duplicate-inverse','actual_order_rejection',duplicate),('false-restoration','rejected_refund',false_success),
      ('orphan-effect','normal_workflow',orphan),('cleanup-only-effect','normal_workflow',cleanup_only),
      ('requester-approval','overlapping_reviewers',self_approve),('missing-reviewer-role','overlapping_reviewers',missing_role),
      ('invented-overlap','overlapping_reviewers',fake_overlap),('two-decisions','overlapping_reviewers',duplicate_decision),
      ('missing-commit','normal_workflow',missing_fence),('stale-without-prior-evidence','displayed_proposal_stale',stale_without_committed_evidence),
      ('reused-authorization','overlapping_reviewers',extra_authorization_use),('early-retry','retry_after',early_retry),
      ('missing-ready-recovery','without_any_broker_delivery',no_ready_recovery))
    with tempfile.TemporaryDirectory(prefix='safeact-b3-replay-counterexamples-') as private:
        for label,fragment,mutate in cases:
            root=Path(private)/label;shutil.copytree(source,root);path=fixture(fragment)
            raw=json.loads(path.read_text());mutate(raw);(root/path.name).write_text(json.dumps(raw))
            try:check(root)
            except AssertionError as error:
                assert str(error)==raw['case'],(label,str(error))
                results.append(dict(counterexample=label,case=raw['case'],rejected=True))
            else:raise AssertionError('Verifier accepted '+label)
    value=dict(scope='Synthetic verifier counterexamples; not provider executions or qualification coverage',cases=len(results),rejected=len(results),results=results)
    Path(a.out).write_text(json.dumps(value,indent=2)+'\n');print('Rejected',len(results),'of',len(cases),'synthetic counterexamples')
if __name__=='__main__':main()
