"""Reject saved-data counterexamples; these are not new executions or tests."""

import argparse
import copy
import importlib.util
import json
from pathlib import Path

from chaos.baselines.registration import RegistrationError

def run(root):
    root=Path(root);spec=importlib.util.spec_from_file_location('stack_replay',root/'replay.py')
    replay=importlib.util.module_from_spec(spec);spec.loader.exec_module(replay)
    values={p.stem:json.loads(p.read_text()) for p in (root/'raw').glob('test_*.json')}
    for value in values.values():replay.check_case(value)
    normal=values['test_registration_blocks_publication_without_holding_database_locks'];cases=[]
    def reject(label,change,base=normal):
        value=copy.deepcopy(base);change(value)
        try:replay.check_case(value)
        except (AssertionError,KeyError,ValueError,RegistrationError):cases.append(dict(case=label,rejected=True))
        else:raise AssertionError('Accepted corrupted saved observations: '+label)
    def event(v,collection,kind):return next(e for e in v['raw'][collection] if e['event']==kind)
    def edit(collection,kind,key,value):return lambda v:event(v,collection,kind).__setitem__(key,value)
    for key in normal['cleanup']:
        reject('cleanup_'+key,lambda v,key=key:v['cleanup'].__setitem__(key,False))
    for kind in ('registration_requested','registration_acknowledged','registration_finished'):
        reject('missing_'+kind,lambda v,kind=kind:v['raw'].__setitem__('registration',[e for e in v['raw']['registration'] if e['event']!=kind]))
    reject('missing_registration_database',lambda v:v['raw'].__setitem__('registration_database',[]))
    reject('wrong_ack_pid',edit('registration','registration_acknowledged','pid',2))
    reject('wrong_ack_xid',edit('registration','registration_acknowledged','commit_xid','1'))
    reject('foreign_ack_schema',edit('registration','registration_acknowledged','schema','safeact_baseline_it_'+'f'*32))
    reject('refused_ack_with_finished_wait',edit('registration','registration_acknowledged','status','refused'))
    reject('stale_ack_time',edit('registration','registration_acknowledged','monotonic_ns',1))
    reject('sensitive_ack_field',edit('registration','registration_acknowledged','password','synthetic'))
    reject('publication_before_registration',edit('broker','publish_started','monotonic_ns',1))
    reject('missing_committed_delivery',lambda v:v['raw'].__setitem__('committed_deliveries',[]))
    reject('missing_real_commit_return',lambda v:v['raw'].__setitem__('execution',[e for e in v['raw']['execution'] if not(e['event']=='commit_returned' and e.get('stage')=='publication')]))
    reject('registration_query_before_request',lambda v:v['raw']['registration_database'][0].__setitem__('sample_started_ns',1))
    for field,value in (('binding_sha256','0'*64),('correlation_sha256','0'*64),('contract_version','q-v2-short')):
        reject('wrong_'+field,lambda v,field=field,value=value:v['raw']['registration_database'][0]['bindings'][0].__setitem__(field,value))
    reject('missing_native_binding',lambda v:v['raw']['registration_database'][0].__setitem__('bindings',[]))
    reject('missing_stopped_generation',lambda v:event(v,'processes','b3_execution_stopped')['processes'].pop())
    reject('drain_before_execution_stop',edit('processes','b3_providers_drained','monotonic_ns',1))
    for component in ('worker','relay'):
        base=values[next(k for k in values if ('prefork_worker_restart' if component=='worker' else 'relay_restart') in k)]
        reject('missing_'+component+'_kill_request',lambda v:v['raw'].__setitem__('processes',[e for e in v['raw']['processes'] if e['event']!='b3_process_kill_requested']),base)
        reject('invented_'+component+'_kill_returncode',edit('processes','b3_process_killed','returncode',0),base)
    base=values['test_lost_publication_ack_replays_registration_and_preserves_intent']
    reject('invented_lost_ack_exception',edit('execution','hook_error','error_type','ValueError'),base)
    base=values['test_held_late_effect_drains_after_execution_stops_before_schema_drop']
    reject('held_effect_before_execution_stops',edit('ledger','effect','monotonic_ns',1),base)
    base=values['test_controller_death_is_retained_and_never_silently_bypassed']
    reject('erased_controller_integrity_failure',lambda v:v['raw'].__setitem__('integrity',[]),base)
    reject('undeclared_controller_failure',lambda v:v['raw'].__setitem__('declared_integrity_failure',False),base)
    return dict(scope='Saved-data mutation refusals only; no qualification runs or extra unittest denominator',
                cases=len(cases),rejected=sum(c['rejected'] for c in cases),mutations=cases)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root');p.add_argument('--out');a=p.parse_args();value=run(a.root)
    if a.out:Path(a.out).write_text(json.dumps(value,indent=2)+'\n')
    print('Rejected',value['rejected'],'of',value['cases'],'saved-data mutations')
