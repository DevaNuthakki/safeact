"""Reject mutations of retained process observations; no provider executions."""

import argparse
import copy
import importlib.util
import json
from pathlib import Path
from uuid import uuid4


def check(root):
    root=Path(root);spec=importlib.util.spec_from_file_location('b3_process_replay',root/'replay.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    values=[json.loads(p.read_text()) for p in (root/'raw').glob('test_*.json')]
    source=lambda fragment:next(v for v in values if fragment in v['case'])
    results=[]
    def reject(label,fragment,mutate):
        value=copy.deepcopy(source(fragment));mutate(value['raw'])
        try:module.check_case(value)
        except (AssertionError,ValueError):results.append(dict(mutation=label,rejected=True))
        else:raise AssertionError('Counterexample accepted: '+label)
    def error(raw):return next(e for e in raw['execution'] if e['event']=='hook_error')
    for field,value in (('commit_xid','999999999'),('pid',1),('error_type','ValueError'),('monotonic_ns',1)):
        reject('ack:'+field,'lost_outcome_ack',lambda r,f=field,v=value:error(r).__setitem__(f,v))
    for field in ('execution','database'):
        reject('ack:missing_'+field,'lost_outcome_ack',lambda r,f=field:r.__setitem__(f,[]))
    reject('ack:missing_error','lost_outcome_ack',lambda r:r.__setitem__('execution',[e for e in r['execution'] if e['event']!='hook_error']))
    for field,value in (('returncode',0),('signal',15),('parked_pid',1)):
        reject('kill:'+field,'sigkill',lambda r,f=field,v=value:r['timeline'][-1].__setitem__(f,v))
    for field in ('timeline','http'):
        reject('kill:missing_'+field,'sigkill',lambda r,f=field:r.__setitem__(f,[]))
    reject('kill:missing_effect','sigkill',lambda r:r.__setitem__('ledger',[e for e in r['ledger'] if e['event']!='effect']))
    reject('kill:missing_request','sigkill',lambda r:r.__setitem__('timeline',[e for e in r['timeline'] if e['event']!='worker_kill_requested']))
    def start(raw):return next(e for e in raw['broker'] if e['event']=='task_started')
    for field in ('task_id','delivery_id'):
        reject('prefork:'+field,'prefork',lambda r,f=field:start(r).__setitem__(f,str(uuid4())))
    reject('prefork:missing_received','prefork',lambda r:r.__setitem__('broker',[e for e in r['broker'] if e['event']!='task_received']))
    reject('prefork:missing_committed_delivery','prefork',lambda r:r.__setitem__('committed_deliveries',[]))
    def same_process(raw):
        parent=next(e['pid'] for e in raw['broker'] if e['event']=='task_received')
        for event in raw['broker']:
            if event['event'] in ('task_started','task_returned'):event['pid']=parent
    reject('prefork:invented_child_process','prefork',same_process)
    def late_publication(raw):
        receive=next(e for e in raw['broker'] if e['event']=='task_received')
        for event in raw['broker']:
            if event['event']=='publish_returned':event['monotonic_ns']=receive['monotonic_ns']+1
    reject('prefork:late_publication','prefork',late_publication)
    reject('workflow:missing_audit','workflow_commit',lambda r:r.__setitem__('workflow_audit',[]))
    reject('workflow:wrong_xid','workflow_commit',lambda r:r['workflow_audit'][0].__setitem__('commit_xid','999999999'))
    reject('workflow:wrong_identity','workflow_commit',lambda r:r['workflow_audit'][0].__setitem__('workflow_id',str(uuid4())))
    reject('http:wrong_digest','all_connectors',lambda r:r['http'][0].__setitem__('correlation_sha256','b'*64))
    reject('http:missing_all_observations','all_connectors',lambda r:r.__setitem__('http',[]))
    reject('http:missing_start','all_connectors',lambda r:r.__setitem__('http',[e for e in r['http'] if e['event']!='http_start']))
    reject('http:invented_fence_xid','all_connectors',lambda r:r.__setitem__('database',[e for e in r['database'] if e['event']!='db_audit_committed']))
    reject('http:secret_field','all_connectors',lambda r:r['http'][0].__setitem__('headers',{'Authorization':'synthetic'}))
    return dict(scope='Synthetic mutations of retained observations, not new unittest cases, provider effects or fault-family coverage',
                cases=len(results),rejected=len(results),results=results)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root');p.add_argument('--out');a=p.parse_args();v=check(a.root)
    if a.out:Path(a.out).write_text(json.dumps(v,indent=2)+'\n')
    print(v['rejected'],'counterexamples rejected')
