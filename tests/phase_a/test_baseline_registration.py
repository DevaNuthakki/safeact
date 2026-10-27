"""Fresh, bounded, private registration acknowledgements cannot be bypassed."""

import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

from chaos.baselines import registration as reg
from chaos.baselines.processes import Config, LostAcknowledgement
from tests.phase_a.test_baseline_processes import fixture


SCHEMA='safeact_baseline_it_'+'a'*32


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.private=tempfile.TemporaryDirectory();self.addCleanup(self.private.cleanup)
        self.directory=Path(self.private.name);self.stop=threading.Event()

    def request(self):return dict(event='registration_requested',schema=SCHEMA,request_id=str(uuid4()),
        pid=os.getpid(),commit_xid='123',monotonic_ns=time.monotonic_ns())

    def wait_request(self):
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            rows=reg.messages(self.directory,SCHEMA,'request')
            if rows:return rows[-1]
            time.sleep(.01)
        self.fail('Registration request not observed')

    def test_schema_directory_and_deadline_guards(self):
        for schema,timeout in (('public',1),(SCHEMA,0),(SCHEMA,True),(SCHEMA,31),(SCHEMA,float('nan'))):
            with self.subTest(schema=schema,timeout=timeout),self.assertRaises(ValueError):reg.Client(self.directory,schema,timeout=timeout)
        self.directory.chmod(0o755)
        with self.assertRaises(ValueError):reg.Client(self.directory,SCHEMA)
        self.directory.chmod(0o700)

    def test_missing_acknowledgement_expires_without_completed_wait(self):
        with self.assertRaises(reg.RegistrationError):reg.Client(self.directory,SCHEMA,timeout=.1).wait('123',self.stop)
        self.assertEqual(len(reg.messages(self.directory,SCHEMA,'request')),1)
        self.assertEqual(reg.messages(self.directory,SCHEMA,'finished'),[])

    def test_exact_fresh_acknowledgement_completes_and_records_actual_interval(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(reg.Client(self.directory,SCHEMA,timeout=2).wait,'123',self.stop)
            request=self.wait_request()
            ack=dict(request,event='registration_acknowledged',status='registered',monotonic_ns=time.monotonic_ns())
            reg.publish_message(self.directory,SCHEMA,'ack',ack)
            self.assertEqual(future.result(timeout=2),request['request_id'])
        rows=reg.messages(self.directory,SCHEMA)
        self.assertEqual([r['event'] for r in rows],['registration_requested','registration_acknowledged','registration_finished'])
        self.assertLess(rows[0]['monotonic_ns'],rows[1]['monotonic_ns']);self.assertLess(rows[1]['monotonic_ns'],rows[2]['monotonic_ns'])

    def test_stale_or_foreign_commit_identity_never_completes(self):
        for field in ('pid','commit_xid'):
            with tempfile.TemporaryDirectory() as directory,ThreadPoolExecutor(max_workers=1) as pool:
                client=reg.Client(directory,SCHEMA,timeout=2);future=pool.submit(client.wait,'123',self.stop)
                until=time.monotonic()+2
                while not (rows:=reg.messages(directory,SCHEMA,'request')) and time.monotonic()<until:time.sleep(.01)
                self.assertTrue(rows);request=rows[0]
                ack=dict(request,event='registration_acknowledged',status='registered',monotonic_ns=time.monotonic_ns())
                ack[field]=os.getpid()+1 if field=='pid' else '124'
                reg.publish_message(directory,SCHEMA,'ack',ack)
                with self.subTest(field=field),self.assertRaises(reg.RegistrationError):future.result(timeout=2)
                self.assertEqual(reg.messages(directory,SCHEMA,'finished'),[])

    def test_refused_acknowledgement_cannot_authorize_publication(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(reg.Client(self.directory,SCHEMA,timeout=2).wait,'123',self.stop)
            request=self.wait_request()
            reg.publish_message(self.directory,SCHEMA,'ack',dict(request,event='registration_acknowledged',status='refused',monotonic_ns=time.monotonic_ns()))
            with self.assertRaises(reg.RegistrationError):future.result(timeout=2)
        self.assertEqual(reg.messages(self.directory,SCHEMA,'finished'),[])

    def test_shutdown_cancels_a_wait_without_a_confirmation(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(reg.Client(self.directory,SCHEMA,timeout=2).wait,'123',self.stop)
            self.wait_request();self.stop.set()
            with self.assertRaises(reg.RegistrationCancelled):future.result(timeout=2)
        self.assertEqual(reg.messages(self.directory,SCHEMA,'finished'),[])

    def test_generation_cannot_be_overwritten_or_reused(self):
        request=self.request();reg.publish_message(self.directory,SCHEMA,'request',request)
        with self.assertRaises(reg.RegistrationError):reg.publish_message(self.directory,SCHEMA,'request',request)
        self.assertEqual(reg.messages(self.directory,SCHEMA,'request'),[request])

    def test_unknown_fields_partial_and_duplicate_json_are_refused(self):
        request=self.request()
        with self.assertRaises(reg.RegistrationError):reg.publish_message(self.directory,SCHEMA,'request',dict(request,password='synthetic'))
        path=self.directory/(request['request_id']+'.request.json')
        for text in ('{',json.dumps(request),json.dumps(request).replace('{','{"pid":1,',1)+'\n'):
            path.write_text(text);path.chmod(0o600)
            with self.subTest(text=text[:20]),self.assertRaises((ValueError,reg.RegistrationError)):reg.messages(self.directory,SCHEMA)

    def test_concurrent_requests_are_complete_unique_and_private(self):
        rows=[self.request() for _ in range(30)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures=[pool.submit(reg.publish_message,self.directory,SCHEMA,'request',r) for r in rows]
            for future in futures:future.result(timeout=3)
        self.assertEqual(len(reg.messages(self.directory,SCHEMA)),30)
        self.assertTrue(all(p.stat().st_mode&0o777==0o600 for p in self.directory.glob('*.json')))

    def test_original_fault_semantics_precede_the_registration_gate(self):
        original,client=Mock(),Mock();hook=reg.gated_hook(original,client,self.stop)
        hook('before_publication_commit',dict(commit_xid='123'));client.wait.assert_not_called()
        hook('after_publication_commit',dict(commit_xid='123'));client.wait.assert_called_once_with('123',self.stop)
        original.side_effect=LostAcknowledgement('synthetic')
        with self.assertRaises(LostAcknowledgement):hook('after_publication_commit',dict(commit_xid='124'))
        self.assertEqual(client.wait.call_count,1)

    def test_optional_registration_config_preserves_the_original_config_shape(self):
        path=self.directory/'config.json';data=fixture();path.write_text(json.dumps(data));path.chmod(0o600)
        self.assertIsNone(Config(path).registration_dir)
        data['registration_dir']=str(self.directory);path.write_text(json.dumps(data))
        self.assertEqual(Config(path).registration_dir,str(self.directory))
        data['unexpected']='value';path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):Config(path)
