"""Own B3 processes/schema; consume the harness slot's broker and providers."""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx2

from chaos.baselines.environment import Environment
from chaos.baselines.observer import Observer
from chaos.baselines.processes import Config, private_path
from chaos.baselines.registration import Controller, messages
from chaos.baselines.store import admit
from chaos.baselines.trace import read_trace
from chaos.baselines.workflow import Workflow
from chaos.quiescence import stop_processes


COMPONENTS=('worker','relay','reconciler','advancer','api')
ROOT=Path(__file__).resolve().parents[2]


class Stack:
    def __init__(self,slot,workdir,*,components=COMPONENTS,api_port=None,execution_mode='b3'):
        if (not components or len(set(components))!=len(components) or set(components)-set(COMPONENTS)
                or (api_port is not None and (type(api_port) is not int or not 0<api_port<65536))):
            raise ValueError('Owned B3 components and a loopback API port required')
        self.slot,self.workdir,self.components=slot,Path(workdir),tuple(components)
        if execution_mode not in ('b1','b2','b3'):raise ValueError('Unknown baseline execution mode')
        self.execution_mode=execution_mode
        self.api_port=api_port;self.env=Environment();self.processes={};self.generations=[];self._logs=[]
        self.controller=None;self.errors=[];self.events=[];self.database=[];self.holds=[]
        self._expected_exits={};self._operations=set();self._workflows=set()
        self._quiescing=False;self._execution_stopped=False;self._drained=False;self._closed=False
        self.config_path=self.workdir/'private-config.json';self.trace_dir=self.workdir/'traces'
        self.registration_dir=self.workdir/'registration';self.log_dir=self.workdir/'logs'
        self.queue='baseline-'+uuid4().hex

    def __repr__(self):return f'BaselineStack(schema={self.env.t.schema!r})'

    @property
    def base_url(self):return f'http://127.0.0.1:{self.api_port}'

    def start(self,timeout=20):
        self.workdir.mkdir(mode=0o700,parents=False,exist_ok=False)
        try:
            for directory in (self.trace_dir,self.registration_dir,self.log_dir):directory.mkdir(mode=0o700)
            self.env.start()
            urls={kind:f'http://127.0.0.1:{port}' for kind,port in self.slot.proxy_ports.items()}
            self.env.write_config(self.config_path,urls,broker_url=self.slot.redis_url,queue=self.queue,
                fault_dir=self.trace_dir,registration_dir=self.registration_dir,execution_mode=self.execution_mode)
            Config(self.config_path)  # Validate all process boundaries before spawning.
            self.controller=Controller(self.env.engine,self.env.t,self.registration_dir,
                self.slot.mocks['payments'].register_key,
                allowed_pids=lambda:{g['process'].pid for g in self.generations if g['component']=='relay'},mode=self.execution_mode).start()
            if 'api' in self.components and self.api_port is None:
                with socket.socket() as probe:probe.bind(('127.0.0.1',0));self.api_port=probe.getsockname()[1]
            for component in self.components:self.spawn(component)
            if 'api' in self.components:
                deadline=time.monotonic()+timeout
                with httpx2.Client(trust_env=False) as client:
                    while time.monotonic()<deadline:
                        self.check_alive()
                        try:
                            if client.get(self.base_url+'/healthz',timeout=.5).status_code==200:break
                        except httpx2.HTTPError:pass
                        time.sleep(.05)
                    else:raise RuntimeError('B3 API did not become ready')
            self.check_alive();return self
        except Exception:
            try:self.close()
            except Exception:self.errors.append('setup_cleanup_failed')
            raise

    def spawn(self,component):
        if component not in self.components or self._quiescing:raise RuntimeError('B3 component cannot be spawned')
        previous=self.processes.get(component)
        if previous is not None and previous.poll() is None:raise RuntimeError('B3 component already running')
        if previous is not None and previous.pid not in self._expected_exits:
            self.errors.append(f'unexpected_exit_before_restart:{component}:{previous.returncode}')
        if previous is not None:self.processes[f'{component}:{previous.pid}']=previous
        command=[sys.executable,'-m','chaos.baselines.processes','--config',str(self.config_path),component]
        if component=='api':command+=['--port',str(self.api_port)]
        path=self.log_dir/f'{component}-{len(self.generations)}.log'
        descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        out=os.fdopen(descriptor,'wb');self._logs.append(out)
        process=subprocess.Popen(command,cwd=ROOT,env={**os.environ,'PYTHONUNBUFFERED':'1'},
                                 stdout=out,stderr=subprocess.STDOUT,start_new_session=True)
        self.processes[component]=process;self.generations.append(dict(component=component,process=process,path=path))
        self.events.append(dict(event='b3_process_started',component=component,pid=process.pid,monotonic_ns=time.monotonic_ns()))
        return process

    def check_alive(self):
        if self.errors:raise RuntimeError('B3 stack retained integrity errors')
        if self.controller is not None and (self.controller.errors or not self.controller.thread.is_alive()):
            self.errors.append('registration_controller_failed')
            raise RuntimeError('B3 registration controller failed')
        for component in self.components:
            process=self.processes.get(component)
            if process is not None and process.poll() is not None and process.pid not in self._expected_exits:
                self.errors.append(f'unexpected_exit_before_quiescence:{component}:{process.returncode}')
                raise RuntimeError(f'B3 {component} exited: {process.returncode}')

    def kill(self,component,sig=signal.SIGKILL):
        process=self.processes[component]
        if sig not in (signal.SIGKILL,signal.SIGTERM) or process.poll() is not None or os.getpgid(process.pid)!=process.pid:
            raise RuntimeError('Refusing an unowned or nonlive B3 process group')
        self.events.append(dict(event='b3_process_kill_requested',component=component,pid=process.pid,
                                signal=int(sig),monotonic_ns=time.monotonic_ns()))
        # The liveness observer can run after killpg() and before wait()
        # returns. Record the owned shutdown request first; prove its result
        # before accepting a replacement. Failed requests remain integrity errors.
        self._expected_exits[process.pid]=None
        try:
            os.killpg(process.pid,sig);code=process.wait(timeout=10)
            if (sig==signal.SIGKILL and code!=-9) or (sig==signal.SIGTERM and code not in (0,-15)):
                raise RuntimeError('B3 process exit did not match its requested signal')
            self._expected_exits[process.pid]=code
        except Exception:
            self._expected_exits.pop(process.pid,None)
            self.errors.append('requested_exit_unproved:'+component)
            raise
        self.events.append(dict(event='b3_process_killed',component=component,pid=process.pid,
                                signal=int(sig),returncode=code,monotonic_ns=time.monotonic_ns()))

    def submit(self,request):
        if self._quiescing or not self.env.created:raise RuntimeError('B3 stack is not accepting intents')
        receipt=admit(self.env.engine,self.env.t,request);self._operations.add(str(receipt.operation_id));return receipt

    def create_workflow(self,request):
        if self._quiescing or not self.env.created:raise RuntimeError('B3 stack is not accepting workflows')
        with Config(self.config_path).runtime() as runtime:
            if self.execution_mode=='b3':receipt=Workflow(runtime).create(request)
            else:
                from chaos.baselines.weak_runtime import QueueWorkflow
                receipt=QueueWorkflow(runtime).create(request)
        self._workflows.add(str(receipt.workflow_id));return receipt

    def snapshot(self):
        try:
            result=Observer(self.env.engine,self.env.t).sample(operation_ids=self._operations,workflow_ids=self._workflows)
            self.database.extend(result.database);return result
        except Exception as error:
            self.errors.append('observation:'+type(error).__name__);raise

    def hold_next(self,provider,*,respond_now=False):
        held=self.slot.mocks[provider].hold_next(respond_now=respond_now);self.holds.append(held);return held

    def stop(self,timeout=7):
        for generation in self.generations:
            process=generation['process']
            if process.poll() is None:
                try:os.killpg(process.pid,signal.SIGTERM)
                except ProcessLookupError:pass
                except OSError as error:self.errors.append('stop_signal:'+type(error).__name__)
        deadline=time.monotonic()+timeout
        for generation in self.generations:
            try:generation['process'].wait(timeout=max(.01,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:self.errors.append('graceful_shutdown_timeout')
        for out in self._logs:out.close()

    def integrity_errors(self):
        result=list(self.errors)
        if self.controller is not None:result+=['registration:'+e for e in self.controller.errors]
        for generation in self.generations:
            text=generation['path'].read_text()
            for marker in ('BASELINE_TRACE_ERROR','SAFEACT_HTTP_TRACE_ERROR','BASELINE_PROCESS_ERROR'):
                if marker in text:result.append(generation['component']+':'+marker)
        return sorted(set(result))

    def quiesce(self):
        if not self._quiescing and self.controller is not None and not self.controller.thread.is_alive():
            self.errors.append('registration_controller_stopped_before_quiescence')
        if not self._quiescing:
            for generation in self.generations:
                process=generation['process']
                if process.poll() is not None and process.pid not in self._expected_exits:
                    self.errors.append(f"unexpected_exit_before_quiescence:{generation['component']}:{process.returncode}")
        self._quiescing=True
        if not self._execution_stopped:
            self.stopped=stop_processes(self,timeout=6)
            if self.controller is not None:self.controller.stop()
            self._execution_stopped=True
            for generation in self.generations:
                p=generation['process'];expected=self._expected_exits.get(p.pid,-15 if generation['component']=='api' else 0)
                if p.poll()!=expected:self.errors.append(f"unexpected_exit:{generation['component']}:{p.poll()}")
            self.events.append(dict(event='b3_execution_stopped',monotonic_ns=time.monotonic_ns(),processes=self.stopped))
        if self.integrity_errors():raise RuntimeError('B3 quiescence retained integrity errors')
        return self.stopped

    def drain(self,holds=()):
        problem=None
        try:self.quiesce()
        except Exception as error:
            if not self._execution_stopped:raise
            problem=error
        if not self._drained:
            self.slot.close_provider_admission()
            all_holds={id(h):h for h in (*self.holds,*holds)}.values()
            for held in all_holds:held.release()
            deadline=time.monotonic()+5;self.slot.wait_provider_idle(max(0,deadline-time.monotonic()))
            for held in all_holds:
                if held.started.is_set() and not held.wait_committed(max(0,deadline-time.monotonic())):
                    raise RuntimeError('B3 held provider effect did not drain')
            self._drained=True;self.events.append(dict(event='b3_providers_drained',monotonic_ns=time.monotonic_ns()))
        if problem is not None:raise RuntimeError('B3 drained with retained integrity errors') from problem

    def close(self,holds=()):
        if self._closed:
            if self.integrity_errors():raise RuntimeError('B3 closed with retained integrity errors')
            return
        problems=[]
        try:self.drain(holds)
        except Exception as error:problems.append(type(error).__name__)
        if self._drained:
            try:self.env.stop()
            except Exception as error:problems.append(type(error).__name__)
            if self.config_path.exists():private_path(self.config_path).unlink()
            self._closed=not self.env.created
        if problems or self.integrity_errors():raise RuntimeError('B3 stack cleanup reported errors')

    def observations(self):
        return dict(database=list(self.database),registration=messages(self.registration_dir,self.env.t.schema),
            registration_database=list(self.controller.events) if self.controller else [],
            processes=list(self.events),**{kind:read_trace(self.trace_dir,self.env.t.schema,kind) for kind in ('http','execution','broker')})
