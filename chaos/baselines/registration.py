"""Harness-owned mock key registration before B3 broker publication."""

import fcntl
import hashlib
import json
import math
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa

from chaos.baselines.store import Request, SCHEMA
from chaos.baselines.keys import registration_keys
from chaos.baselines.trace import check_file, directory_fd, unique_object, uuid_text
from chaos.profiles import profile
from connectors.http_trace import correlation_hash


class RegistrationError(RuntimeError):pass
class RegistrationCancelled(RegistrationError):pass


@contextmanager
def locked(directory):
    root=directory_fd(directory);fd=None
    try:
        flags=os.O_RDWR|os.O_NONBLOCK|os.O_NOFOLLOW
        try:fd=os.open('.registration.lock',flags|os.O_CREAT|os.O_EXCL,0o600,dir_fd=root)
        except FileExistsError:fd=os.open('.registration.lock',flags,dir_fd=root)
        check_file(fd);deadline=time.monotonic()+1
        while True:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise RegistrationError('Registration lock deadline expired')
                time.sleep(.01)
        yield root
    finally:
        if fd is not None:os.close(fd)
        os.close(root)


SUFFIXES={'request':'registration_requested','ack':'registration_acknowledged','finished':'registration_finished'}


def validate(row,schema,suffix):
    fields={'event','schema','request_id','pid','commit_xid','monotonic_ns'}
    if suffix!='request':fields.add('status')
    if (not isinstance(row,dict) or set(row)!=fields or row.get('event')!=SUFFIXES[suffix]
            or row.get('schema')!=schema or not uuid_text(row.get('request_id'))
            or type(row.get('pid')) is not int or row['pid']<=1
            or type(row.get('monotonic_ns')) is not int or row['monotonic_ns']<=0
            or type(row.get('commit_xid')) is not str or not row['commit_xid'].isascii()
            or not row['commit_xid'].isdigit() or not 0<int(row['commit_xid'])<2**64
            or str(int(row['commit_xid']))!=row['commit_xid']
            or (suffix!='request' and row.get('status') not in ('registered','refused'))
            or (suffix=='finished' and row.get('status')!='registered')):
        raise RegistrationError('Invalid registration message')


def read_at(root,name):
    fd=os.open(name,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW,dir_fd=root)
    with os.fdopen(fd,'rb') as stream:
        check_file(stream.fileno());data=stream.read(4097)
    if len(data)>4096 or not data.endswith(b'\n'):raise RegistrationError('Invalid registration message size')
    return json.loads(data,object_pairs_hook=unique_object)


def publish_message(directory,schema,suffix,row):
    validate(row,schema,suffix)
    data=(json.dumps(row,sort_keys=True,allow_nan=False)+'\n').encode()
    if len(data)>4096:raise RegistrationError('Registration message too large')
    name=f"{row['request_id']}.{suffix}.json";temporary='.pending-'+str(uuid4())
    with locked(directory) as root:
        try:
            os.stat(name,dir_fd=root,follow_symlinks=False)
        except FileNotFoundError:pass
        else:raise RegistrationError('Registration generation already exists')
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=root)
        try:
            try:
                check_file(fd)
                if os.write(fd,data)!=len(data):raise RegistrationError('Incomplete registration message write')
            finally:os.close(fd)
            os.rename(temporary,name,src_dir_fd=root,dst_dir_fd=root)
        finally:
            try:os.unlink(temporary,dir_fd=root)
            except FileNotFoundError:pass


def messages(directory,schema,suffix=None):
    result=[]
    with locked(directory) as root:
        for name in sorted(os.listdir(root)):
            if not name.endswith('.json') or name.startswith('.'):continue
            parts=name.split('.')
            if len(parts)!=3 or parts[1] not in SUFFIXES or not uuid_text(parts[0]):
                raise RegistrationError('Unknown registration file')
            if suffix is not None and parts[1]!=suffix:continue
            row=read_at(root,name);validate(row,schema,parts[1])
            if row['request_id']!=parts[0]:raise RegistrationError('Registration filename mismatch')
            result.append(row)
    return sorted(result,key=lambda row:row['monotonic_ns'])


class Client:
    def __init__(self,directory,schema,*,timeout=10):
        if (type(schema) is not str or not SCHEMA.fullmatch(schema)
                or type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=30):
            raise ValueError('Owned B3 schema and bounded registration deadline required')
        os.close(directory_fd(directory));self.directory,self.schema,self.timeout=Path(directory),schema,timeout

    def wait(self,commit_xid,stopping):
        if stopping.is_set():raise RegistrationCancelled('Relay stopping')
        start=time.monotonic();request=dict(event=SUFFIXES['request'],schema=self.schema,request_id=str(uuid4()),
            pid=os.getpid(),commit_xid=commit_xid,monotonic_ns=time.monotonic_ns())
        publish_message(self.directory,self.schema,'request',request)
        while time.monotonic()-start<self.timeout:
            if stopping.is_set():raise RegistrationCancelled('Relay stopping')
            with locked(self.directory) as root:
                try:ack=read_at(root,f"{request['request_id']}.ack.json")
                except FileNotFoundError:ack=None
            if ack is not None:
                validate(ack,self.schema,'ack')
                if any(ack[k]!=request[k] for k in ('request_id','schema','pid','commit_xid')) or ack['monotonic_ns']<request['monotonic_ns']:
                    raise RegistrationError('Stale or foreign registration acknowledgement')
                if ack['status']!='registered':raise RegistrationError('Mock registration refused')
                publish_message(self.directory,self.schema,'finished',dict(ack,event=SUFFIXES['finished'],monotonic_ns=time.monotonic_ns()))
                return request['request_id']
            stopping.wait(.02)
        raise RegistrationError('Mock registration deadline expired')


class Controller:
    def __init__(self,engine,tables,directory,register_key,*,allowed_pids=None,mode='b3'):
        self.engine,self.t,self.directory=engine,tables,Path(directory)
        Client(directory,tables.schema)
        self.register_key,self.allowed_pids=register_key,allowed_pids
        if mode not in ('b1','b2','b3'):raise ValueError('Unknown baseline registration strategy')
        self.mode=mode
        self.registered={};self.handled=set();self.events=[];self.errors=[]
        self.stopping=threading.Event();self.thread=None;self._tick_lock=threading.Lock()

    def tick(self):
        with self._tick_lock:
            for request in messages(self.directory,self.t.schema,'request'):
                if request['request_id'] in self.handled:continue
                try:
                    if self.allowed_pids is not None and request['pid'] not in self.allowed_pids():
                        # Popen can return before the owner records the new generation.
                        # An unknown pid is never acknowledged during that bounded gap.
                        if 0<=time.monotonic_ns()-request['monotonic_ns']<1_000_000_000:continue
                        raise RegistrationError('Registration from an unowned relay')
                    self.register(request)
                except Exception as error:
                    self.errors.append(type(error).__name__)
                    publish_message(self.directory,self.t.schema,'ack',dict(request,event=SUFFIXES['ack'],
                        status='refused',monotonic_ns=time.monotonic_ns()))
                    self.handled.add(request['request_id'])
                    raise RegistrationError('Mock registration controller failed') from None
                publish_message(self.directory,self.t.schema,'ack',dict(request,event=SUFFIXES['ack'],
                    status='registered',monotonic_ns=time.monotonic_ns()))
                self.handled.add(request['request_id'])

    def register(self,request):
        started=time.monotonic_ns();fields=tuple(Request.__dataclass_fields__)+('payload_sha256',)
        with self.engine.connect().execution_options(isolation_level='REPEATABLE READ') as c:
            with c.begin():
                c.exec_driver_sql('SET TRANSACTION READ ONLY')
                rows=c.execute(sa.select(self.t.jobs.c.operation_id,*(self.t.jobs.c[k] for k in fields))
                    .order_by(self.t.jobs.c.operation_id)).mappings().all()
        finished=time.monotonic_ns();bindings=[]
        if not rows:raise RegistrationError('Publication has no committed job bindings')
        current={str(row['operation_id']) for row in rows}
        if self.registered.keys()-current:raise RegistrationError('Registered operation disappeared')
        for row in rows:
            op=str(row['operation_id']);binding=tuple(row[k] for k in fields)
            expected=Request(**{k:row[k] for k in Request.__dataclass_fields__}).immutable()
            if any(row[k]!=v for k,v in expected.items()):raise RegistrationError('Invalid immutable binding')
            if op in self.registered and self.registered[op]!=binding:raise RegistrationError('Registered operation binding changed')
            contract=profile(row['connector_id'],row['contract_version'])
            if contract.contract.native_idempotency:
                for key in registration_keys(row['operation_id'],row['connector_id'],row['contract_version'],self.mode):
                    if op not in self.registered:self.register_key(key,row['contract_version'])
                    bindings.append(dict(operation_id=op,connector_id=row['connector_id'],contract_version=row['contract_version'],
                        correlation_sha256=correlation_hash(key),binding_sha256=hashlib.sha256(json.dumps(expected,sort_keys=True).encode()).hexdigest()))
            self.registered[op]=binding
        self.events.append(dict(event='registration_database_sample',source='database',request_id=request['request_id'],
            schema=self.t.schema,pid=request['pid'],commit_xid=request['commit_xid'],sample_started_ns=started,
            sample_finished_ns=finished,monotonic_ns=finished,registration_finished_ns=time.monotonic_ns(),bindings=bindings))

    def start(self):
        if self.thread is not None:raise RegistrationError('Controller already started')
        def run():
            try:
                while not self.stopping.is_set():self.tick();self.stopping.wait(.02)
            except Exception as error:
                if not self.errors:self.errors.append(type(error).__name__)
                self.stopping.set()
        self.thread=threading.Thread(target=run,name='b3-mock-registration',daemon=True);self.thread.start()
        return self

    def stop(self,timeout=6):
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(timeout)
            if self.thread.is_alive():raise RegistrationError('Registration controller did not stop')


def gated_hook(hook,client,stopping):
    def observed(point,details):
        hook(point,details)
        if point=='after_publication_commit':client.wait(details['commit_xid'],stopping)
    return observed
