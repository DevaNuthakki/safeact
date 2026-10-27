"""Real B3 F14 HTTP participants, native records and PostgreSQL wait graphs."""

import secrets
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx2
import psycopg
from psycopg import sql

from chaos.baselines import boundary_observer
from chaos.database_trace import _interval
from storage.database import local_admin_settings


def require(condition,message):
    if not condition:raise RuntimeError(message)


class Boundaries:
    def prepare_review(self,scenario):
        require(scenario.review_mode in ('concurrent','stale','protected_retry'),'Unknown native B3 review mode')
        password='local-review-'+secrets.token_urlsafe(20);self._tokens={}
        actors={'qualification':('requester',),'investigator':('investigator',),'reviewer':('reviewer',),'reviewer2':('reviewer',)}
        for actor,roles in actors.items():
            self.env.provision_human(actor_id=actor,tenant_id='tenant-north',username=actor,password=password,roles=roles)
            with httpx2.Client(base_url=self.stack.base_url,trust_env=False,timeout=5) as client:
                response=client.post('/login',json=dict(username=actor,password=password))
                require(response.status_code==200,'B3 human login failed');self._tokens[actor]=response.json()['token']
        if scenario.review_mode=='protected_retry':self.env.provision_review_policy('mock-crm',scenario.contract_version)

    def _boundary_event(self,event,started=None,**details):
        finished=time.monotonic_ns()
        self.boundary_events.append(dict(_interval(started or finished,finished),event=event,**details))

    def _human(self,actor,method,path,body=None,*,label,proposal=None):
        started=time.monotonic_ns()
        with httpx2.Client(base_url=self.stack.base_url,trust_env=False,timeout=8) as client:
            response=client.request(method,path,json=body,headers={'Authorization':'Bearer '+self._tokens[actor]})
        native=response.json();allowed={'status','proposal_id','operation_id','proposer_id','action','basis','binding_sha256','expires_at','error'}
        require(isinstance(native,dict) and not set(native)-allowed,'Unexpected B3 review response fields')
        details=dict(source='boundary_http',actor_id=actor,status_code=response.status_code,boundary_format=boundary_observer.FORMAT,native_body=native)
        if proposal:
            details.update(proposal_id=proposal['proposal_id'],binding_digest=proposal['binding_sha256'])
        elif 'proposal_id' in native:
            details.update(proposal_id=native['proposal_id'],binding_digest=native.get('binding_sha256'))
        if body and 'decision_key' in body:details['decision_key']=body['decision_key']
        status=native.get('status')
        if status in ('accepted','replayed'):details['outcome']='approved' if status=='accepted' else 'replayed'
        if status in ('stale','already_decided'):details['error']='stale_approval' if status=='stale' else status
        self._boundary_event(label,started,**details)
        return response.status_code,native

    def _review_snapshot(self,local_id,label):
        value=boundary_observer.sample(self.env.engine,self.env.t,local_id,label)
        with self.lock:self.database.append(value)
        return value

    def _wait_review(self,local_id,state,*,negative=False):
        deadline=time.monotonic()+18
        while time.monotonic()<deadline:
            sample=self._review_snapshot(local_id,'wait_state')
            if sample['state']==state and (not negative or any(e['native_kind']=='not_applied' for e in sample['evidence'])):return sample
            time.sleep(.05)
        raise RuntimeError('B3 review did not independently observe '+state)

    def _decide(self,actor,local_id,proposal,key,*,label='decision_response'):
        status,body=self._human(actor,'POST',f"/actions/{local_id}/proposals/{proposal['proposal_id']}/decisions",
            dict(binding_sha256=proposal['binding_sha256'],decision_key=key,approved=True),label=label,proposal=proposal)
        return actor,key,status,body

    def _race(self,local_id,proposal):
        with psycopg.connect(**local_admin_settings()) as blocker:
            blocker.execute(sql.SQL('SELECT operation_id FROM {}.baseline_jobs WHERE operation_id=%s FOR UPDATE').format(
                sql.Identifier(self.env.t.schema)),(local_id,))
            root=blocker.info.backend_pid
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(self._decide,actor,local_id,proposal,str(uuid4())) for actor in ('reviewer','reviewer2')]
                try:
                    deadline=time.monotonic()+3
                    while True:
                        started=time.monotonic_ns()
                        with psycopg.connect(**local_admin_settings(),autocommit=True) as observer:
                            waiters=observer.execute("SELECT pid,pg_blocking_pids(pid) FROM pg_stat_activity WHERE usename=%s AND wait_event_type='Lock'",(self.env.role,)).fetchall()
                        blocked={root}
                        for _ in waiters:blocked.update(pid for pid,parents in waiters if blocked.intersection(parents))
                        pids=sorted(blocked-{root})
                        if len(pids)==2:
                            self._boundary_event('reviewers_blocked',started,source='database',operation_id=local_id,
                                proposal_id=proposal['proposal_id'],api_backend_pids=pids,blocker_pid=root,
                                blocking_edges=[dict(pid=pid,blocked_by=parents) for pid,parents in waiters]);break
                        require(time.monotonic()<deadline,'Two B3 HTTP decisions did not overlap on the actual job lock')
                        require(not any(f.done() for f in futures),'A B3 reviewer returned before lock overlap')
                        time.sleep(.01)
                finally:blocker.rollback()
                return [f.result() for f in futures]

    def _reveal_review(self,emit):
        mock=self.slot.mocks['crm'];mock.fail_after_effect=False;mock.index_lag=False;mock.reindex()
        emit('boundary_provider_revealed',provider='crm')

    def drive_boundary(self,scenario,local_ids,emit):
        if scenario.family!='F14':return
        require(len(local_ids)==1,'B3 human-review admission did not identify one operation')
        local_id=local_ids[0];retry=scenario.review_mode=='protected_retry'
        self._wait_review(local_id,'UNKNOWN' if retry else 'ESCALATED_UNRESOLVED',negative=retry)
        process=self.stack.processes['reconciler'];self.stack.kill('reconciler',signal.SIGTERM)
        emit('boundary_reconciler_stopped',pid=process.pid,returncode=process.returncode)
        status,proposal=self._human('investigator','POST',f'/actions/{local_id}/proposals',
            dict(action='PROTECTED_RETRY' if retry else 'REVERIFY'),label='proposal_created')
        require(status==200 and proposal['status']=='proposed','B3 investigator proposal was refused')
        for actor in ('reviewer','reviewer2'):
            status,display=self._human(actor,'GET',f"/actions/{local_id}/proposals/{proposal['proposal_id']}",
                label='proposal_displayed')
            require(status==200 and display['binding_sha256']==proposal['binding_sha256'] and display['basis']==proposal['basis'],
                'B3 reviewers did not display the exact persisted binding')
        self._review_snapshot(local_id,'displayed_binding')
        if scenario.review_mode=='stale':
            self._reveal_review(emit);self.stack.spawn('reconciler');self._wait_review(local_id,'SUCCEEDED')
            refused=self._decide('reviewer',local_id,proposal,str(uuid4()))
            require(refused[2]==409 and refused[3]['status']=='stale','B3 stale displayed binding was not refused')
            sample=self._review_snapshot(local_id,'stale_refused');require(not sample['decisions'],'Stale B3 review committed a decision')
        else:
            results=self._race(local_id,proposal)
            winners=[r for r in results if r[2]==200 and r[3].get('status')=='accepted']
            losers=[r for r in results if r[2]==409 and r[3].get('status')=='already_decided']
            require(len(winners)==len(losers)==1,'B3 review did not produce one winner and one refusal')
            winner=winners[0];repeated=self._decide(winner[0],local_id,proposal,winner[1],label='decision_replayed')
            require(repeated[2]==200 and repeated[3]['status']=='replayed','B3 winning decision did not replay')
            sample=self._review_snapshot(local_id,'race_decided');require(len(sample['decisions'])==1,'B3 race decision count changed')
            self._reveal_review(emit);self.stack.spawn('reconciler');self._wait_review(local_id,'SUCCEEDED')
        self._review_snapshot(local_id,'review_complete')
