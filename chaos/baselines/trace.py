"""Private B3 observations. These records are never provider-effect evidence."""

import base64
import fcntl
import json
import math
import os
import re
import stat
import time
from contextvars import ContextVar
from pathlib import Path
from uuid import UUID, uuid4

from chaos.baselines.store import SCHEMA


ERROR = b"BASELINE_TRACE_ERROR\n"
TASK_NAME = "baseline.execute"
IDS = ("operation_id","outbox_id","delivery_id","task_id")
POINTS = frozenset({f"{side}_{stage}_commit" for side in ("before","after")
    for stage in ("claim","fence","outcome","investigation","lookup","publication","publication_ack",
                  "workflow_admission","workflow_advance","proposal","decision","delivery")}
    | {"before_provider_call","after_provider_call"})
EVENTS = dict(http=frozenset({"http_start","http_response","http_error"}),
    execution=frozenset({"hook_enter","commit_before","commit_returned","hook_error"}),
    broker=frozenset({"publish_started","publish_returned","task_received","task_started","task_returned"}))
STATUSES = frozenset({"recorded","replayed","stale_observed","not_claimable","not_fenced",
                     "request_deadline_elapsed","ack_lost"})
PUBLISHING = ContextVar("b3_trace_publishing",default=None)


def failed():
    try:os.write(2,ERROR)
    except OSError:pass


def uuid_text(value):
    return type(value) is str and str(UUID(value))==value


def identity(args, task_id):
    if not isinstance(args,(tuple,list)) or len(args)!=3 or not all(uuid_text(v) for v in (*args,task_id)):
        raise ValueError("Four canonical durable task identities required")
    return dict(zip(IDS,(*args,task_id)))


def validate(kind,row):
    common={"event","source","pid","at","monotonic_ns"}
    if (not isinstance(row,dict) or row.get("event") not in EVENTS[kind]
            or row.get("source")!={"http":"http_client"}.get(kind,kind)
            or type(row.get("pid")) is not int or row["pid"]<=0
            or type(row.get("monotonic_ns")) is not int or row["monotonic_ns"]<=0
            or type(row.get("at")) not in (int,float) or not math.isfinite(row["at"])):
        raise ValueError("Invalid B3 observation")
    event=row["event"]
    if kind=="http":
        fields={"request_id","provider","method","path","correlation_sha256"}
        if (re.fullmatch(r"[0-9a-f]{32}",row.get("request_id","")) is None
                or re.fullmatch(r"[0-9a-f]{64}",row.get("correlation_sha256","")) is None
                or row.get("provider") not in ("payments","crm","email")
                or (row.get("provider"),row.get("method"),row.get("path")) not in {
                    ("payments","POST","/v1/refunds"),("payments","POST","/v1/charges"),
                    ("payments","GET","/v1/lookup"),("crm","POST","/records"),
                    ("crm","GET","/records"),("email","POST","/messages")}):
            raise ValueError("Invalid B3 HTTP identity")
        if event=="http_response":
            fields.add("status_code")
            if type(row.get("status_code")) is not int or not 100<=row["status_code"]<=599:
                raise ValueError("Invalid HTTP response code")
        if event=="http_error":fields.add("error_type")
    elif kind=="execution":
        fields={"point"}
        if row.get("point") not in POINTS:raise ValueError("Invalid B3 hook point")
        fields.update(k for k in ("operation_id","workflow_id","attempt_id") if k in row)
        if not all(uuid_text(row[k]) for k in fields-{"point"}):raise ValueError("Invalid B3 execution identity")
        if row["point"].endswith("_commit"):
            fields.update(("stage","commit_xid"))
            if (row.get("stage")!=row["point"].split("_",1)[1][:-7]
                    or re.fullmatch(r"[1-9][0-9]{0,19}",row.get("commit_xid","")) is None):
                raise ValueError("Invalid B3 commit identity")
        if event in ("commit_before","commit_returned") and not row["point"].startswith(
                "before_" if event=="commit_before" else "after_"):
            raise ValueError("Commit observation on wrong hook side")
        if event in ("commit_before","commit_returned") and "stage" not in fields:
            raise ValueError("Commit observation without a transaction")
        if event=="hook_error":fields.add("error_type")
    else:
        fields={*IDS,"queue","redelivered"}
        identity([row.get(k) for k in IDS[:3]],row.get("task_id"))
        if (re.fullmatch(r"baseline-[a-z0-9-]{8,80}",row.get("queue","")) is None
                or type(row.get("redelivered")) is not bool):raise ValueError("Invalid B3 delivery")
        if "task_status" in row:
            fields.add("task_status")
            if event!="task_returned" or row["task_status"] not in STATUSES:raise ValueError("Invalid B3 task status")
    if "error_type" in fields and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,127}",row.get("error_type","")) is None:
        raise ValueError("Invalid exception type")
    if set(row)!=common|fields:raise ValueError("Unexpected or missing B3 observation fields")


def directory_fd(directory):
    fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    info=os.fstat(fd)
    if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:
        os.close(fd);raise ValueError("B3 trace directory must be private and owned")
    return fd


def check_file(fd):
    info=os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
            or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1):
        raise ValueError("B3 trace must be a private owned regular file with one link")


def unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError("Duplicate B3 observation field")
        result[key]=value
    return result


def append(directory,name,row):
    data=(json.dumps(row,sort_keys=True,allow_nan=False)+"\n").encode()
    if len(data)>4096:raise ValueError("Oversized B3 observation")
    root=directory_fd(directory)
    flags=os.O_WRONLY|os.O_APPEND|os.O_NONBLOCK|os.O_NOFOLLOW
    try:
        try:fd=os.open(name,flags|os.O_CREAT|os.O_EXCL,0o600,dir_fd=root)
        except FileExistsError:fd=os.open(name,flags,dir_fd=root)
    finally:os.close(root)
    try:
        check_file(fd);fcntl.flock(fd,fcntl.LOCK_EX)
        if os.write(fd,data)!=len(data):raise OSError("Partial B3 trace write")
    finally:os.close(fd)


class Trace:
    def __init__(self,directory,schema,kind):
        if type(schema) is not str or not SCHEMA.fullmatch(schema) or kind not in EVENTS:
            raise ValueError("B3 observations require an owned disposable B3 schema and known protocol")
        os.close(directory_fd(directory));self.directory,self.kind=Path(directory),kind

    def __call__(self,event,**details):
        row=dict(details,event=event,source={"http":"http_client"}.get(self.kind,self.kind),
                 pid=os.getpid(),at=time.time(),monotonic_ns=time.monotonic_ns())
        validate(self.kind,row);append(self.directory,f"{self.kind}-{os.getpid()}.jsonl",row)

    def emit(self,event,**details):
        try:self(event,**details)
        except Exception:failed()


def read_trace(directory,schema,kind,since_ns=0):
    Trace(directory,schema,kind)
    if type(since_ns) is not int or since_ns<0:raise ValueError("Invalid observation cutoff")
    root=directory_fd(directory);rows=[];seen=set()
    try:
        for name in sorted(os.listdir(root)):
            if not name.startswith(kind+"-") or not name.endswith(".jsonl"):continue
            match=re.fullmatch(kind+r"-([1-9][0-9]*)\.jsonl",name)
            if not match:raise ValueError("Invalid B3 trace filename")
            fd=os.open(name,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW,dir_fd=root)
            with os.fdopen(fd,"rb") as stream:
                check_file(stream.fileno());fcntl.flock(stream.fileno(),fcntl.LOCK_SH)
                while line:=stream.readline(4097):
                    if len(line)>4096 or not line.endswith(b"\n"):raise ValueError("Incomplete or oversized B3 trace record")
                    row=json.loads(line,object_pairs_hook=unique_object);validate(kind,row)
                    if row["pid"]!=int(match[1]):raise ValueError("B3 trace process mismatch")
                    key=(row["pid"],row["monotonic_ns"],row["event"],
                         row.get("request_id",row.get("task_id",row.get("point"))))
                    if key in seen:raise ValueError("Duplicate B3 trace observation")
                    seen.add(key)
                    if row["monotonic_ns"]>=since_ns:rows.append(row)
    finally:os.close(root)
    return sorted(rows,key=lambda row:row["monotonic_ns"])


def queued_message(envelope,queue):
    if not isinstance(envelope,bytes) or len(envelope)>65536:raise ValueError("Invalid B3 broker envelope")
    message=json.loads(envelope,object_pairs_hook=unique_object);headers,props=message["headers"],message["properties"]
    if (headers["task"]!=TASK_NAME or message["content-type"]!="application/json"
            or message["content-encoding"]!="utf-8" or props["body_encoding"]!="base64"
            or props["correlation_id"]!=headers["id"] or props["delivery_info"]["routing_key"]!=queue):
        raise ValueError("Unexpected B3 broker envelope")
    body=json.loads(base64.b64decode(message["body"],validate=True),object_pairs_hook=unique_object)
    if not isinstance(body,list) or len(body)!=3 or body[1]!={}:raise ValueError("Unexpected B3 task body")
    return identity(body[0],headers["id"])


def publish(app,task,queue):
    task_id=str(uuid4());token=PUBLISHING.set((app,task_id))
    try:
        app.send_task(TASK_NAME,args=[str(task.operation_id),str(task.outbox_id),str(task.delivery_id)],
                      queue=queue,task_id=task_id)
    finally:PUBLISHING.reset(token)
    return task_id


def install(app,directory,schema,queue,*,role,signals=None):
    if role not in ("publisher","worker"):raise ValueError("Unknown B3 observer role")
    writer=Trace(directory,schema,"broker")
    if signals is None:from celery import signals
    connections=[]
    def connect(name,callback):
        signal=getattr(signals,name);signal.connect(callback,weak=False);connections.append((signal,callback))
    def publication(event):
        def observe(sender=None,headers=None,body=None,routing_key=None,**kwargs):
            active=PUBLISHING.get()
            if not active or active[0] is not app or sender!=TASK_NAME or routing_key!=queue:return
            try:
                if headers['id']!=active[1]:raise ValueError("Publication task identity changed")
                writer.emit(event,**identity(body[0],headers['id']),queue=queue,redelivered=False)
            except Exception:failed()
        return observe
    def execution(event):
        def observe(task=None,task_id=None,args=None,**kwargs):
            if task is None or task.app is not app or task.name!=TASK_NAME:return
            try:
                info=task.request.delivery_info
                if info.get('routing_key')!=queue:return
                status={}
                if event=='task_returned' and kwargs.get('state')=='SUCCESS':status['task_status']=kwargs.get('retval')
                writer.emit(event,**identity(args,task_id),queue=queue,redelivered=info.get('redelivered',False),**status)
            except Exception:failed()
        return observe
    def received(request=None,**kwargs):
        try:
            if request is None or request.name!=TASK_NAME or request.task.app is not app:return
            info=request.delivery_info
            if info.get('routing_key')==queue:
                writer.emit('task_received',**identity(request.args,request.id),queue=queue,
                            redelivered=info.get('redelivered',False))
        except Exception:failed()
    if role=='publisher':
        connect('before_task_publish',publication('publish_started'));connect('after_task_publish',publication('publish_returned'))
    else:
        connect('task_received',received);connect('task_prerun',execution('task_started'));connect('task_postrun',execution('task_returned'))
    return connections
