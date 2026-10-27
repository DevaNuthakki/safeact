"""Foreign tasks/queues cannot enter baseline broker fault controls."""

import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from chaos.broker_loss import QueuedLoss
from chaos.broker_protocol import protocol
from chaos.broker_redelivery import DispatchRedelivery, unacked_message
from tests.phase_a.test_qualification_broker import envelope as safeact_envelope


QUEUE='baseline-'+uuid4().hex
IDS=[str(uuid4()) for _ in range(4)]


def envelope():
    return dict(headers=dict(task='baseline.execute',id=IDS[3]),properties=dict(body_encoding='base64',
        correlation_id=IDS[3],delivery_tag=IDS[3],delivery_info=dict(routing_key=QUEUE)),
        **{'content-type':'application/json','content-encoding':'utf-8'},
        body=base64.b64encode(json.dumps([IDS[:3],{},{}]).encode()).decode())


class BaselineBrokerProtocolTests(unittest.TestCase):
    def test_real_protocol_identity_round_trip(self):
        value=protocol('baseline').decode(json.dumps(envelope()).encode(),QUEUE)
        self.assertEqual(list(value.values()),IDS)
        unacked=json.dumps([envelope(),QUEUE,QUEUE]).encode()
        self.assertEqual(unacked_message(IDS[3].encode(),unacked,QUEUE,protocol='baseline'),value)

    def test_foreign_task_or_routing_never_decodes(self):
        for changes in ('task','queue','correlation','content','arguments'):
            row=envelope()
            if changes=='task':row['headers']['task']='safeact.execute_operation'
            if changes=='queue':row['properties']['delivery_info']['routing_key']='baseline-'+uuid4().hex
            if changes=='correlation':row['properties']['correlation_id']=str(uuid4())
            if changes=='content':row['content-type']='application/x-python-serialize'
            if changes=='arguments':row['body']=base64.b64encode(json.dumps([IDS[:3],{'payload':'synthetic'},{}]).encode()).decode()
            with self.subTest(changes=changes),self.assertRaises(ValueError):protocol('baseline').decode(json.dumps(row).encode(),QUEUE)

    def test_safeact_default_stays_separate_from_baseline(self):
        with self.assertRaises(ValueError):protocol('safeact').decode(json.dumps(envelope()).encode(),QUEUE)
        with self.assertRaises(ValueError):protocol('baseline').decode(safeact_envelope(),QUEUE)
        with self.assertRaises(ValueError):unacked_message(IDS[3].encode(),json.dumps([envelope(),QUEUE,QUEUE]).encode(),QUEUE)

    def test_duplicate_json_and_noncanonical_ids_are_refused(self):
        row=envelope();row['headers']['id']='not-a-uuid';row['properties']['correlation_id']='not-a-uuid'
        with self.assertRaises(ValueError):protocol('baseline').decode(json.dumps(row).encode(),QUEUE)
        text=json.dumps(envelope()).replace('{','{"body":"duplicate",',1)
        with self.assertRaises(ValueError):protocol('baseline').decode(text.encode(),QUEUE)

    def test_fault_controls_still_require_a_reserved_slot_database(self):
        for db in (1,9,True):
            client=Mock();client.connection_pool.connection_kwargs={'db':db}
            with self.subTest(db=db),self.assertRaises(RuntimeError):QueuedLoss(None,client,db,QUEUE,None,None,None,None,protocol='baseline')
            with self.assertRaises(RuntimeError):DispatchRedelivery(client,db,QUEUE,None,None,None,None,None,protocol='baseline')

    def test_baseline_is_explicit_and_unknown_protocols_fail_closed(self):
        client=Mock();client.connection_pool.connection_kwargs={'db':8}
        with self.assertRaises(RuntimeError):QueuedLoss(None,client,8,QUEUE,None,None,None,None)
        with self.assertRaises(ValueError):protocol('unknown')
        with self.assertRaises(RuntimeError):QueuedLoss(None,client,8,'baseline-unowned',None,None,None,None,protocol='baseline')

    def test_generic_safeact_read_does_not_grant_a_nonreserved_queue_to_fault_drivers(self):
        from tests.phase_a.test_broker_redelivery import TAG, unacked
        original = json.loads(unacked())
        queue = 'safeact-redelivery-test-'+uuid4().hex
        original[0]['properties']['delivery_info']['routing_key'] = queue
        original[2] = queue
        decoded = unacked_message(TAG.encode(), json.dumps(original).encode(), queue)
        self.assertTrue(decoded['operation_id'])
        client=Mock();client.connection_pool.connection_kwargs={'db':8}
        with self.assertRaises(RuntimeError):
            DispatchRedelivery(client,8,queue,None,None,None,None,None)
