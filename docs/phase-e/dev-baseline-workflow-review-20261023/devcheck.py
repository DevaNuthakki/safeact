"""Retain selected real integration observations; not a qualification arm.

Run from the repository root with PYTHONPATH=. and the explicit local DB opt-ins.
"""
import argparse,json,os,platform,sys,time,unittest
from pathlib import Path
import sqlalchemy as sa
from connectors.http_trace import ObservedClient
from deploy.evaluation.agent_v1.secret_scan import scan_text
from tests.integration.test_baseline_workflow import BaselineWorkflowTests
from tests.integration.test_baseline_review import BaselineReviewTests

TABLES=('workflows','steps','workflow_audit','jobs','attempts','observations','audit','outbox','deliveries',
        'accounts','policies','proposals','decisions','authorizations','authorization_uses')
CASES=(
 (BaselineWorkflowTests,'test_normal_workflow_has_two_distinct_forward_effects_and_no_inverse'),
 (BaselineWorkflowTests,'test_actual_order_rejection_refunds_exact_original_charge_and_amount'),
 (BaselineWorkflowTests,'test_lost_refund_reply_is_verified_without_a_second_effect'),
 (BaselineWorkflowTests,'test_hidden_refund_effect_remains_unresolved_without_false_restoration'),
 (BaselineWorkflowTests,'test_rejected_refund_keeps_forward_history_and_unresolved_compensation'),
 (BaselineWorkflowTests,'test_uncertain_order_never_admits_a_refund'),
 (BaselineWorkflowTests,'test_uncertain_charge_does_not_admit_an_order_until_actual_positive_lookup'),
 (BaselineReviewTests,'test_real_overlapping_reviewers_commit_one_decision_winner_key_replays'),
 (BaselineReviewTests,'test_actual_new_matching_provider_evidence_makes_displayed_proposal_stale'),
 (BaselineReviewTests,'test_reverify_is_one_lookup_past_actual_horizon_and_never_a_dispatch'),
 (BaselineReviewTests,'test_late_lookup_resolves_approved_ready_work_without_any_broker_delivery'),
 (BaselineReviewTests,'test_human_approval_preserves_actual_retry_after_and_avoids_unnecessary_lookup'),
)
def dump(path,value):path.write_text(json.dumps(value,indent=2,default=str)+'\n')
def snapshot(case,label):
    start=time.monotonic_ns()
    with case.env.engine.begin() as c:
        c.exec_driver_sql('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        value={n:[dict(r) for r in c.execute(sa.select(getattr(case.t,n))).mappings()] for n in TABLES}
        value['humans']=[dict(r) for r in c.execute(sa.select(case.t.humans.c.actor_id,case.t.humans.c.tenant_id,
            case.t.humans.c.roles,case.t.humans.c.enabled)).mappings()]
        role=c.scalar(sa.text('SELECT current_user'))
        observed=c.scalar(sa.select(sa.func.clock_timestamp()))
    row=dict(event='independent_database_snapshot',label=label,source='database',application_role=role,
             sample_started_ns=start,sample_finished_ns=time.monotonic_ns(),database_observed_at=observed,tables=value)
    case.saved['snapshots'].append(row)
    return row

def instrument(base,method):
    class Retained(base):
        def setUp(self):
            super().setUp()
            self.saved=dict(case=method,scope='Selected B3 integration development observations; not a registered comparison arm',
                            snapshots=[],timeline=[],boundary=[],http=[],ledger=[],harness_errors=[])
            if hasattr(self,'config'):
                trace=Path(self.config).parent/'trace';trace.mkdir(mode=0o700)
                path=Path(self.config).parent/'observed-private.json'
                self.env.write_config(path,self.servers.urls,fault_dir=trace)
                self.config=path;self.trace=trace
            old=self.runtime.hook
            def hook(point,details):
                self.saved['timeline'].append(dict(event=point,monotonic_ns=time.monotonic_ns(),**details))
                if point=='before_provider_call':snapshot(self,'provider_fence_visible')
                old(point,details)
            self.runtime.hook=hook
            def http(event,**details):
                self.saved['http'].append(dict(event=event,source='http',pid=os.getpid(),monotonic_ns=time.monotonic_ns(),**details))
            for connector in self.runtime.connectors.values():
                provider=connector._provider
                provider._http=ObservedClient(connector.connector_id,provider._http,http)
            snapshot(self,'initial')
        def call(self,method,path,*,actor='investigator',body=None):
            snapshot(self,'before_human_request')
            start=time.monotonic_ns()
            response=super().call(method,path,actor=actor,body=body)
            end=time.monotonic_ns()
            self.saved['boundary'].append(dict(event='human_http_request',actor_id=actor,method=method,path=path,
                request=body,response=response.json(),status_code=response.status_code,started_ns=start,finished_ns=end))
            snapshot(self,'after_human_request')
            return response
        def tearDown(self):
            if hasattr(self,'saved') and self.env.created:
                snapshot(self,'observation_window_end')
                self.saved['observation_window_end_ns']=time.monotonic_ns()
                self.saved['ledger_before_cleanup']=self.ledger.events()
                self.saved['review_overlap']=getattr(self,'review_overlap',None)
                self.saved['process_trace']=[json.loads(line) for p in self.trace.glob('trace-*.jsonl')
                                             for line in p.read_text().splitlines()]
                if getattr(self,'log',None):self.log.flush();self.saved['api_log']=self.log_path.read_text()
            super().tearDown()
    return Retained(method)

def main():
    p=argparse.ArgumentParser(description=__doc__.splitlines()[0]);p.add_argument('--out',required=True);args=p.parse_args()
    if any(os.environ.get(k)!='1' for k in ('SAFEACT_RUN_BASELINE_DEV_CHECK','SAFEACT_RUN_DB_INTEGRATION','SAFEACT_RUN_DISPOSABLE_DB_TESTS')):
        raise RuntimeError('Explicit local B3 development and disposable DB opt-ins required')
    out=Path(args.out);out.mkdir(parents=True,exist_ok=False);rows=[]
    for base,method in CASES:
        case=instrument(base,method);result=unittest.TestResult();started=time.monotonic();case.run(result)
        raw=getattr(case,'saved',dict(case=method,snapshots=[],ledger=[],harness_errors=[]))
        raw['harness_errors']=[dict(kind='failure' if source is result.failures else 'error',trace=trace)
                               for source in (result.failures,result.errors) for _,trace in source]
        raw['skipped']=list(result.skipped);raw['passed']=result.wasSuccessful() and not result.skipped
        raw['ledger']=case.ledger.events() if hasattr(case,'ledger') else []
        process=getattr(case,'process',None)
        raw['cleanup']=dict(schema_dropped=not getattr(getattr(case,'env',None),'created',True),
            private_files_removed=not Path(case.config).parent.exists() if hasattr(case,'config') else False,
            process_stopped=process is None or process.poll() is not None,
            process_exit_code=process.returncode if process else None)
        raw['elapsed_seconds']=time.monotonic()-started
        text=json.dumps(raw,default=str)
        private=[getattr(case,'password',None),getattr(getattr(case,'env',None),'password',None)]+list(getattr(case,'tokens',{}).values())
        if any(secret and secret in text for secret in private):raise RuntimeError('Private credential found; refusing evidence retention')
        if scan_text(text,suffix='.json'):raise RuntimeError('Secret scanner rejected development evidence')
        dump(out/(method+'.json'),raw);rows.append(raw)
        print(method,'PASS' if raw['passed'] else 'FAIL',flush=True)
    summary=dict(scope='Selected B3 integration development checks; not qualification, comparison or load results',
        cases=len(rows),passed=sum(r['passed'] for r in rows),errors=sum(len(r['harness_errors']) for r in rows),
        cleanup_complete=all(all(r['cleanup'][k] for k in ('schema_dropped','private_files_removed','process_stopped')) for r in rows),
        effects=sum(sum(e['event']=='effect' for e in r['ledger']) for r in rows),
        dispatch_calls=sum(sum(e['event']=='provider_start' and e.get('method') in ('refund','charge','create_record','send') for e in r['ledger']) for r in rows),
        lookup_calls=sum(sum(e['event']=='provider_start' and e.get('method') in ('lookup','search') for e in r['ledger']) for r in rows),
        external_provider_calls=0,paid_model_calls=0,
        environment=dict(platform=platform.system(),python=platform.python_version(),load_average=os.getloadavg()))
    dump(out/'summary.json',summary);print(json.dumps(summary),flush=True)
    return int(summary['passed']!=summary['cases'] or not summary['cleanup_complete'])
if __name__=='__main__':sys.exit(main())
