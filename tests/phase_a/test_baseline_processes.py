"""Private mock-only baseline process boundary and observable one-shot faults."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from chaos.baselines.processes import Config,Hook,LostAcknowledgement,celery_app


def fixture():
    return dict(schema="safeact_baseline_it_"+"0"*32,
        database=dict(host="127.0.0.1",port=5432,dbname="safeact_dev",user="safeact_base_"+"0"*32+"_app",password="synthetic-example"),
        providers={k:"http://127.0.0.1:8000" for k in ("payments","crm","email")},
        broker_url="redis://:synthetic-example@127.0.0.1:6379/9",queue="baseline-example-private",fault_dir=None)


class BaselineProcessTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path=Path(self.directory.name)/"private.json"

    def config(self,value=None):
        self.path.write_text(json.dumps(value or fixture()))
        self.path.chmod(0o600)
        return Config(self.path)

    def test_credentials_are_private_and_not_exposed_by_repr(self):
        config=self.config()
        self.assertNotIn("synthetic-example",repr(config))
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):Config(self.path)

    def test_symlinked_config_is_refused(self):
        self.config()
        link=Path(self.directory.name)/"link.json"
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):Config(link)

    def test_public_database_schema_or_administrator_role_is_refused(self):
        for field,value in (("schema","public"),("role","safeact_dev"),("host","db.example.com")):
            data=fixture()
            if field=="schema":data[field]=value
            else:data["database"]["user" if field=="role" else field]=value
            with self.assertRaises(ValueError):self.config(data)

    def test_execution_cannot_be_pointed_at_real_or_credential_bearing_http_services(self):
        for url in ("https://api.stripe.com","http://example.com:8000","http://user:pass@127.0.0.1:8000","http://127.0.0.1:8000/path"):
            data=fixture();data["providers"]["payments"]=url
            with self.assertRaises(ValueError):self.config(data)

    def test_demo_default_and_external_brokers_are_refused(self):
        for url in ("redis://:example@127.0.0.1:6379/0","redis://:example@127.0.0.1:6379/1",
                    "redis://:example@127.0.0.1:6379/16","redis://:example@redis.example.com:6379/9"):
            data=fixture();data["broker_url"]=url
            with self.assertRaises(ValueError):self.config(data)

    def test_transport_has_late_acknowledgement_and_json_only_delivery(self):
        app=celery_app(self.config())
        self.addCleanup(app.close)
        self.assertTrue(app.conf.task_acks_late)
        self.assertTrue(app.conf.task_reject_on_worker_lost)
        self.assertEqual(app.conf.accept_content,["json"])
        self.assertEqual(app.conf.worker_prefetch_multiplier,1)
        self.assertIsNone(app.conf.result_backend)

    def test_one_shot_fault_records_its_actual_firing_without_credentials(self):
        directory=Path(self.directory.name)
        armed=directory/"after_fence_commit"
        armed.write_text("lost_ack");armed.chmod(0o600)
        hook=Hook(directory)
        with self.assertRaises(LostAcknowledgement):hook("after_fence_commit",dict(commit_xid="123"))
        hook("after_fence_commit",dict(commit_xid="124"))
        records=[json.loads(line) for line in next(directory.glob("trace-*.jsonl")).read_text().splitlines()]
        self.assertEqual([r["event"] for r in records],["hook","fault_fired","hook"])
        self.assertEqual(records[1]["action"],"lost_ack")
        self.assertEqual(records[1]["commit_xid"],"123")
        self.assertEqual(os.stat(next(directory.glob("trace-*.jsonl"))).st_mode & 0o777,0o600)

    def test_unknown_hook_point_cannot_read_an_arbitrary_file(self):
        hook=Hook(self.directory.name)
        with self.assertRaises(ValueError):hook("../../private.json",{})


if __name__=="__main__":unittest.main()
