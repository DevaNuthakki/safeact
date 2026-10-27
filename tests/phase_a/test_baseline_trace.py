"""B3's trace boundary rejects unsafe files, forged fields and cross-app signals."""

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from chaos.baselines import trace
from chaos.baselines.processes import Hook, LostAcknowledgement, celery_app
from chaos.coverage import observed
from deploy.stack.http_trace import HttpTrace
from tests.phase_a.test_execution_trace import commit_run


SCHEMA='safeact_baseline_it_'+'a'*32


class BaselineTraceTests(unittest.TestCase):
    def setUp(self):
        self.private=tempfile.TemporaryDirectory();self.addCleanup(self.private.cleanup)
        self.directory=Path(self.private.name)

    def writer(self,kind='http'):return trace.Trace(self.directory,SCHEMA,kind)
    def rows(self,kind='http'):return trace.read_trace(self.directory,SCHEMA,kind)
    def http(self,writer=None):
        (writer or self.writer())('http_start',request_id=uuid4().hex,provider='crm',method='POST',
                                path='/records',correlation_sha256='a'*64)

    def test_private_schema_directory_and_safeact_guard_remain_distinct(self):
        for schema in ('public','safeact_demo','safeact_it_'+'a'*32):
            with self.subTest(schema=schema),self.assertRaises(ValueError):trace.Trace(self.directory,schema,'http')
        with self.assertRaises(RuntimeError):HttpTrace(self.directory,SCHEMA)
        self.directory.chmod(0o755)
        with self.assertRaises(ValueError):self.writer()
        self.directory.chmod(0o700)
        link=self.directory/'link';link.symlink_to(self.directory,target_is_directory=True)
        with self.assertRaises(OSError):trace.Trace(link,SCHEMA,'http')

    def test_roundtrip_retains_protocol_identity_and_private_file_mode(self):
        self.http();row=self.rows()[0]
        self.assertEqual((row['source'],row['pid']),('http_client',os.getpid()))
        path=next(self.directory.glob('http-*.jsonl'))
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertEqual(trace.read_trace(self.directory,SCHEMA,'http',row['monotonic_ns']+1),[])

    def test_headers_payloads_and_exception_messages_cannot_enter_a_record(self):
        for field in ('headers','password','payload','error_message','raw_key'):
            with self.subTest(field=field),self.assertRaises(ValueError):
                self.writer()('http_error',request_id=uuid4().hex,provider='crm',method='POST',path='/records',
                    correlation_sha256='a'*64,error_type='ReadTimeout',**{field:'private-synthetic-content'})
        self.assertFalse(list(self.directory.glob('http-*')))

    def test_nonprivate_symlink_hardlink_and_fifo_files_are_refused(self):
        path=self.directory/f'http-{os.getpid()}.jsonl';target=self.directory/'target'
        for kind in ('public','symlink','hardlink','fifo'):
            with self.subTest(kind=kind):
                if kind=='public':path.write_text('');path.chmod(0o644)
                elif kind=='symlink':target.write_text('');target.chmod(0o600);path.symlink_to(target)
                elif kind=='hardlink':target.write_text('');target.chmod(0o600);os.link(target,path)
                else:os.mkfifo(path,0o600)
                with self.assertRaises((OSError,ValueError)):self.http()
                with self.assertRaises((OSError,ValueError)):self.rows()
                path.unlink()
                if target.exists():target.unlink()

    def test_duplicate_partial_oversized_and_duplicate_json_fields_are_refused(self):
        self.http();path=next(self.directory.glob('http-*'));line=path.read_text()
        for value in (line+line,line.rstrip('\n'),'x'*4097+'\n',line.replace('{','{"pid":1,',1)):
            path.write_text(value)
            with self.subTest(case=value[:35]),self.assertRaises(ValueError):self.rows()

    def test_process_filename_and_unknown_record_fields_are_refused(self):
        self.http();path=next(self.directory.glob('http-*'));row=self.rows()[0]
        for key,value in (('pid',os.getpid()+1),('monotonic_ns',True),('source','provider'),('extra','value')):
            changed=dict(row,**{key:value});path.write_text(json.dumps(changed)+'\n')
            with self.subTest(key=key),self.assertRaises(ValueError):self.rows()

    def test_concurrent_appenders_leave_complete_distinct_records(self):
        writer=self.writer()
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures=[pool.submit(self.http,writer) for _ in range(40)]
            for future in futures:future.result(timeout=5)
        rows=self.rows();self.assertEqual(len(rows),40)
        self.assertEqual(len({r['request_id'] for r in rows}),40)

    def test_observer_failure_marks_harness_without_raising_into_business_code(self):
        writer=self.writer('execution')
        with patch('chaos.baselines.trace.append',side_effect=OSError('private message')),patch('chaos.baselines.trace.failed') as failed:
            self.assertIsNone(writer.emit('hook_enter',point='before_provider_call'))
        failed.assert_called_once_with()

    def test_lost_ack_records_native_exception_and_the_same_actual_xid(self):
        hook=Hook(self.directory,SCHEMA);op=str(uuid4())
        hook('before_outcome_commit',dict(operation_id=op,commit_xid='123'))
        path=self.directory/'after_outcome_commit';path.write_text('lost_ack');path.chmod(0o600)
        with self.assertRaises(LostAcknowledgement):hook('after_outcome_commit',dict(operation_id=op,commit_xid='123'))
        rows=self.rows('execution')
        self.assertEqual([r['event'] for r in rows],['hook_enter','commit_before','hook_enter','commit_returned','hook_error'])
        self.assertEqual(rows[-1]['error_type'],'LostAcknowledgement')
        self.assertTrue(all(r['commit_xid']=='123' and r['operation_id']==op for r in rows))

    def test_hook_drops_unexpected_sensitive_details_from_both_trace_formats(self):
        hook=Hook(self.directory,SCHEMA)
        with patch('chaos.baselines.processes.failed') as failed:
            hook('before_provider_call',dict(operation_id=str(uuid4()),password='private-synthetic-content'))
        failed.assert_called_once_with()
        self.assertTrue(self.rows('execution'))
        self.assertNotIn('private-synthetic-content',''.join(p.read_text() for p in self.directory.glob('*.jsonl')))

    def test_native_lost_ack_needs_the_same_committed_xid_order_and_process(self):
        raw=commit_run();raw['execution'][-1]['error_type']='LostAcknowledgement'
        self.assertIn('commit_ack_lost',observed(raw))
        for key,value in (('commit_xid','999'),('pid',999),('error_type','ValueError'),('monotonic_ns',5)):
            candidate=commit_run();candidate['execution'][-1]['error_type']='LostAcknowledgement'
            candidate['execution'][-1][key]=value
            with self.subTest(key=key):self.assertNotIn('commit_ack_lost',observed(candidate))

    def test_actual_publisher_signals_are_scoped_to_app_and_envelope_identity(self):
        from celery import Celery
        queue='baseline-'+uuid4().hex;connections=[]
        apps=[Celery('b3-trace-'+uuid4().hex,broker='memory://',set_as_current=False) for _ in range(2)]
        task=SimpleNamespace(operation_id=uuid4(),outbox_id=uuid4(),delivery_id=uuid4())
        try:
            for app in apps:
                app.conf.update(task_default_queue=queue,task_ignore_result=True,accept_content=['json'])
                connections+=trace.install(app,self.directory,SCHEMA,queue,role='publisher')
            result=trace.publish(apps[0],task,queue)
            rows=self.rows('broker')
            self.assertEqual([r['event'] for r in rows],['publish_started','publish_returned'])
            self.assertTrue(all(r['task_id']==result for r in rows))
            with apps[0].connection_for_read() as connection:
                with connection.channel() as channel:message=channel._get(queue)
            decoded=trace.queued_message(json.dumps(message).encode(),queue)
            self.assertEqual(decoded['task_id'],result)
            message['headers']['task']='safeact.execute_operation'
            with self.assertRaises(ValueError):trace.queued_message(json.dumps(message).encode(),queue)
        finally:
            for signal,callback in connections:signal.disconnect(callback)
            for app in apps:app.close()

    def test_closing_app_disconnects_worker_and_publisher_observers(self):
        from celery import signals
        for role,names in (('publisher',('before_task_publish','after_task_publish')),
                           ('worker',('task_received','task_prerun','task_postrun'))):
            before={name:list(getattr(signals,name).receivers) for name in names}
            config=SimpleNamespace(broker='memory://',queue='baseline-'+uuid4().hex,trace_dir=self.directory,
                                   tables=SimpleNamespace(schema=SCHEMA))
            app=celery_app(config,role=role)
            self.assertTrue(all(len(getattr(signals,name).receivers)==len(before[name])+1 for name in names))
            app.close()
            self.assertTrue(all(getattr(signals,name).receivers==before[name] for name in names))
