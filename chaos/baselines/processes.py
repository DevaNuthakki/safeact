"""Private B3 worker, relay and reconciler processes; local mocks only."""

import argparse
import fcntl
import json
import os
import re
import signal
import stat
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx2
import sqlalchemy as sa
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from chaos.baselines.runtime import Runtime, Task
from chaos.baselines.store import Tables
from chaos.baselines.trace import POINTS, TASK_NAME, Trace, append, failed, install, publish, uuid_text
from connectors.crm import CrmConnector
from connectors.email import EmailConnector
from connectors.http_providers import HttpCrmProvider, HttpEmailProvider, HttpPaymentsProvider
from connectors.payments import PaymentsConnector


def private_path(path, *, directory=False):
    path = Path(path)
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ValueError("Baseline configuration/observations must be private and owned")
    return path


class LostAcknowledgement(ConnectionError):
    pass


class Hook:
    def __init__(self, directory, schema=None):
        self.directory = private_path(directory,directory=True) if directory else None
        self.execution = Trace(self.directory,schema,"execution") if self.directory and schema else None

    def emit(self, event, point, details):
        row = dict(details,event=event,point=point,pid=os.getpid(),monotonic_ns=time.monotonic_ns())
        try:append(self.directory,f"trace-{os.getpid()}.jsonl",row)
        except Exception:failed()

    def __call__(self, point, details):
        if self.directory is None:
            return
        if point not in POINTS:
            raise ValueError("Unknown baseline hook point")
        allowed = {"operation_id","workflow_id","attempt_id","commit_xid"}
        clean = {}
        if set(details)-allowed:failed()
        for key,value in details.items():
            if key not in allowed:continue
            try:
                valid = (value is None or (bool(re.fullmatch(r"[1-9][0-9]{0,19}",value))
                         if key=="commit_xid" else uuid_text(value)))
            except (ValueError,TypeError):valid=False
            if valid:clean[key]=value
            else:failed()
        observed = {k:v for k,v in clean.items() if v is not None}
        if point.endswith("_commit"):observed["stage"] = point.split("_",1)[1][:-7]
        if self.execution:
            self.execution.emit("hook_enter",point=point,**observed)
            if point.endswith("_commit"):
                self.execution.emit("commit_before" if point.startswith("before_") else "commit_returned",point=point,**observed)
        try:return self.fire(point,clean)
        except Exception as error:
            if self.execution:self.execution.emit("hook_error",point=point,error_type=type(error).__name__,**observed)
            raise

    def fire(self,point,details):
        self.emit("hook",point,details)
        armed = self.directory/point
        taken = self.directory/f".{point}-{os.getpid()}-{time.monotonic_ns()}"
        try:
            os.rename(armed,taken)
        except FileNotFoundError:
            return
        action = private_path(taken).read_text().strip()
        if action not in ("lost_ack","crash","park"):raise ValueError("Unknown baseline failpoint action")
        self.emit("fault_fired",point,dict(details,action=action))
        if action=="lost_ack":
            raise LostAcknowledgement("Synthetic baseline commit acknowledgement loss")
        if action=="crash":
            os.kill(os.getpid(),signal.SIGKILL)
        if action=="park":
            # Publish only after the complete marker is closed. A watcher may
            # run between open() and write(); a visible empty file is not proof.
            temporary = self.directory/f'.parked-{os.getpid()}-{time.monotonic_ns()}'
            try:
                fd = os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,"w") as stream:
                    stream.write(json.dumps(dict(point=point,pid=os.getpid(),**details)))
                lock = os.open(self.directory/'.park-publish.lock',
                    os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
                try:
                    private_path(self.directory/'.park-publish.lock')
                    deadline = time.monotonic()+1
                    while True:
                        try:
                            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise RuntimeError('Parked-marker publication lock expired')
                            time.sleep(.01)
                    if (self.directory/'parked').exists():
                        raise RuntimeError('Another baseline process is already parked')
                    os.rename(temporary,self.directory/'parked')
                finally:
                    os.close(lock)
            finally:
                temporary.unlink(missing_ok=True)
            threading.Event().wait()
        raise ValueError("Unknown baseline failpoint action")


class Config:
    def __init__(self, path):
        path = private_path(path)
        with path.open() as stream:
            value = json.load(stream)
        fields={"schema","database","providers","broker_url","queue","fault_dir"}
        if not fields<=set(value)<=fields|{'registration_dir','execution_mode'}:
            raise ValueError("Unexpected baseline configuration")
        self.tables = Tables(value["schema"])
        self.execution_mode=value.get('execution_mode','b3')
        if self.execution_mode not in ('b1','b2','b3'):raise ValueError('Unknown baseline execution mode')
        self.database = value["database"]
        if self.database.get("host")!="127.0.0.1" or not re.fullmatch(r"safeact_base_[0-9a-f]{32}_app",self.database.get("user","")):
            raise ValueError("Baseline processes require a local unprivileged baseline role")
        self.providers = value["providers"]
        if set(self.providers)!={"payments","crm","email"}:
            raise ValueError("All three mock provider URLs are required")
        for url in self.providers.values():
            p = urlsplit(url)
            if (p.scheme!="http" or p.hostname!="127.0.0.1" or p.username or p.password
                    or p.path not in ("","/") or p.query or p.fragment or p.port is None):
                raise ValueError("Baseline execution is restricted to local HTTP mocks")
        self.broker = value["broker_url"]
        self.queue = value["queue"]
        if self.broker is not None:
            p = urlsplit(self.broker)
            if (p.scheme!="redis" or p.hostname!="127.0.0.1" or p.username or not p.password
                    or p.query or p.fragment or not re.fullmatch(r"/[2-9]|/1[0-5]",p.path)
                    or not re.fullmatch(r"baseline-[a-z0-9-]{8,80}",self.queue or "")):
                raise ValueError("Private qualification/test broker and queue required")
        self.trace_dir = value["fault_dir"]
        self.hook = Hook(self.trace_dir,self.tables.schema)
        self.registration_dir=value.get('registration_dir')
        if self.registration_dir is not None:
            from chaos.baselines.registration import Client
            Client(self.registration_dir,self.tables.schema)

    def __repr__(self):
        return f"BaselineConfig(schema={self.tables.schema!r})"

    @contextmanager
    def runtime(self):
        db = self.database
        engine = sa.create_engine(URL.create("postgresql+psycopg",username=db["user"],password=db["password"],
            host=db["host"],port=db["port"],database=db["dbname"]),
            poolclass=NullPool,isolation_level="READ COMMITTED",hide_parameters=True)
        clients = [httpx2.Client(base_url=self.providers[k],trust_env=False,follow_redirects=False)
                   for k in ("payments","crm","email")]
        try:
            observer = Trace(self.trace_dir,self.tables.schema,"http") if self.trace_dir else None
            connectors = (PaymentsConnector(HttpPaymentsProvider(self.providers["payments"],client=clients[0],observer=observer)),
                CrmConnector(HttpCrmProvider(self.providers["crm"],client=clients[1],observer=observer)),
                EmailConnector(HttpEmailProvider(self.providers["email"],client=clients[2],observer=observer)))
            if self.execution_mode=='b3':runtime=Runtime(engine,self.tables,connectors,hook=self.hook)
            else:
                from chaos.baselines.weak_runtime import QueueRuntime
                runtime=QueueRuntime(engine,self.tables,connectors,hook=self.hook,mode=self.execution_mode)
            yield runtime
        finally:
            for client in clients:
                client.close()
            engine.dispose()


def celery_app(config, *, role=None):
    from celery import Celery
    if config.broker is None:
        raise ValueError("A private broker is required")
    app = Celery("baseline",broker=config.broker,set_as_current=False)
    app.conf.update(task_acks_late=True,task_reject_on_worker_lost=True,
        task_acks_on_failure_or_timeout=True,worker_prefetch_multiplier=1,
        task_serializer="json",accept_content=["json"],task_ignore_result=True,
        result_backend=None,task_default_queue=config.queue,broker_connection_retry_on_startup=True,
        broker_transport_options={"visibility_timeout":12},worker_hijack_root_logger=False)

    @app.task(name=TASK_NAME,shared=False)
    def execute(operation_id,outbox_id,delivery_id):
        try:
            with config.runtime() as runtime:
                return runtime.execute(Task(*(UUID(i) for i in (operation_id,outbox_id,delivery_id))))
        except LostAcknowledgement:
            return "ack_lost"
        except Exception as error:
            raise RuntimeError(f"BASELINE_PROCESS_ERROR:{type(error).__name__}") from None
    if role is not None and config.trace_dir:
        connections = install(app,config.trace_dir,config.tables.schema,config.queue,role=role)
        close = app.close
        def close_observed():
            for sig,callback in connections:sig.disconnect(callback)
            connections.clear();close()
        app.close = close_observed
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config",required=True)
    parser.add_argument("component",choices=("worker","relay","reconciler","advancer","api","execute"))
    parser.add_argument("--task",nargs=3)
    parser.add_argument("--port",type=int)
    args = parser.parse_args(argv)
    config = Config(args.config)
    if args.component=="api":
        if not args.port or not 1<=args.port<=65535:
            raise ValueError("An explicit loopback API port is required")
        import uvicorn
        from chaos.baselines.review_api import create_app
        with config.runtime() as runtime:
            uvicorn.run(create_app(runtime),host="127.0.0.1",port=args.port,
                        log_config=None,access_log=False,log_level="warning")
        return 0
    if args.component=="worker":
        import billiard
        # The installed prefork stack uses fork on macOS too; connections and
        # clients are constructed lazily inside each task, after the fork.
        billiard.set_start_method("fork",force=True)
        app = celery_app(config,role="worker")
        try:
            app.start(["--quiet","worker","--pool=prefork","--concurrency=2","--loglevel=WARNING",
                       "--without-heartbeat","--without-mingle","--without-gossip"])
        finally:app.close()
        return 0
    if args.component=="execute":
        if not args.task:
            raise ValueError("A committed delivery identity is required")
        with config.runtime() as runtime:
            runtime.execute(Task(*(UUID(i) for i in args.task)))
        return 0
    stopping = threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stopping.set())
    signal.signal(signal.SIGINT,lambda *_:stopping.set())
    with config.runtime() as runtime:
        app = celery_app(config,role="publisher") if args.component=="relay" else None
        from chaos.baselines.registration import Client, RegistrationCancelled, gated_hook
        if app and config.registration_dir is not None:
            runtime.hook=gated_hook(runtime.hook,Client(config.registration_dir,config.tables.schema),stopping)
        if args.component=="advancer":
            from chaos.baselines.workflow import Workflow
            if config.execution_mode=='b3':workflow=Workflow(runtime)
            else:
                from chaos.baselines.weak_runtime import QueueWorkflow
                workflow=QueueWorkflow(runtime)
        try:
            while not stopping.is_set():
                try:
                    if app:runtime.relay_once(lambda task:publish(app,task,config.queue))
                    elif args.component=="advancer":workflow.advance_once()
                    else:runtime.reconcile_once()
                except LostAcknowledgement:
                    pass
                except RegistrationCancelled:
                    if not stopping.is_set():raise
                    break
                stopping.wait(.2)
        finally:
            if app:app.close()
    return 0


if __name__=="__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"BASELINE_PROCESS_ERROR:{type(error).__name__}",file=sys.stderr)
        sys.exit(1)
