"""Owned B3 one-shot faults; native process identities remain in evidence."""

import fcntl
import json
import os
import re
import signal
import time
from pathlib import Path
from uuid import uuid4

from chaos.baselines.trace import POINTS, check_file, directory_fd, unique_object, uuid_text


def arm(directory,point,action):
    if point not in POINTS or action not in ('lost_ack','park','crash'):raise ValueError('Unknown baseline fault')
    root=directory_fd(directory)
    lock=None;temporary='.armed-'+uuid4().hex
    try:
        flags=os.O_RDWR|os.O_NONBLOCK|os.O_NOFOLLOW
        try:lock=os.open('.fault-arm.lock',flags|os.O_CREAT|os.O_EXCL,0o600,dir_fd=root)
        except FileExistsError:lock=os.open('.fault-arm.lock',flags,dir_fd=root)
        check_file(lock);deadline=time.monotonic()+1
        while True:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise RuntimeError('Baseline fault arming lock expired')
                time.sleep(.01)
        try:os.stat(point,dir_fd=root,follow_symlinks=False)
        except FileNotFoundError:pass
        else:raise RuntimeError('Baseline fault is already armed')
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=root)
        with os.fdopen(fd,'w') as stream:check_file(stream.fileno());stream.write(action)
        os.rename(temporary,point,src_dir_fd=root,dst_dir_fd=root)
    finally:
        try:os.unlink(temporary,dir_fd=root)
        except FileNotFoundError:pass
        if lock is not None:os.close(lock)
        os.close(root)


def fired(directory):
    result=[];root=directory_fd(directory)
    try:
        for name in sorted(os.listdir(root)):
            if not name.startswith('trace-') or not name.endswith('.jsonl'):continue
            if re.fullmatch(r'trace-[1-9][0-9]*\.jsonl', name) is None:raise ValueError('Invalid baseline fault filename')
            pid=int(name[6:-6]);last=0
            fd=os.open(name,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW,dir_fd=root)
            with os.fdopen(fd,'rb') as stream:
                check_file(stream.fileno());fcntl.flock(stream.fileno(),fcntl.LOCK_SH)
                while line:=stream.readline(4097):
                    if len(line)>4096 or not line.endswith(b'\n'):raise ValueError('Incomplete baseline fault record')
                    row=json.loads(line,object_pairs_hook=unique_object)
                    allowed={'event','point','pid','monotonic_ns','operation_id','workflow_id','attempt_id','commit_xid','action'}
                    if (set(row)-allowed or row.get('event') not in ('hook','fault_fired') or type(row.get('pid')) is not int or row['pid']!=pid
                            or row.get('point') not in POINTS or type(row.get('monotonic_ns')) is not int or row['monotonic_ns']<=last
                            or any(v is not None and not uuid_text(v) for k,v in row.items() if k.endswith('_id'))
                            or ('commit_xid' in row and row['commit_xid'] is not None
                                and (type(row['commit_xid']) is not str or re.fullmatch(r'[1-9][0-9]{0,19}',row['commit_xid']) is None))):
                        raise ValueError('Invalid baseline fault record')
                    last=row['monotonic_ns']
                    if row['event']=='fault_fired':
                        if row.get('action') not in ('lost_ack','park','crash'):raise ValueError('Invalid baseline fault action')
                        result.append(row)
    finally:os.close(root)
    return sorted(result,key=lambda r:r['monotonic_ns'])


def kill_parked(stack,point,emit):
    root=directory_fd(stack.trace_dir)
    try:
        fd=os.open('parked',os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW,dir_fd=root)
        with os.fdopen(fd) as stream:
            check_file(stream.fileno());data=stream.read(4097)
        if len(data)>4096:raise ValueError('Oversized baseline parked record')
        row=json.loads(data,object_pairs_hook=unique_object)
        if (set(row)-{'point','pid','operation_id','attempt_id','workflow_id','commit_xid'}
                or row.get('point')!=point or type(row.get('pid')) is not int or row['pid']<=1
                or not all(v is None or uuid_text(v) for k,v in row.items() if k.endswith('_id'))):
            raise ValueError('Invalid baseline parked record')
        child=row['pid'];worker=stack.processes['worker']
        if worker.poll() is not None or os.getpgid(child)!=worker.pid:raise RuntimeError('Parked child is outside the owned worker group')
        details=dict(point=point,worker_pid=worker.pid,parked_pid=child,process_group=worker.pid,signal=int(signal.SIGKILL))
        emit('worker_kill_requested',**details);stack.kill('worker',signal.SIGKILL)
        emit('worker_killed',**details,returncode=worker.returncode)
        os.unlink('parked',dir_fd=root)
        replacement=stack.spawn('worker');emit('worker_restarted',worker_pid=replacement.pid,previous_pid=worker.pid)
    finally:os.close(root)
