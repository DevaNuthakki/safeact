"""Ownership, process-kill evidence, recovery and failed-cleanup counterexamples."""

import copy
import io
import json
import os
import queue
import tempfile
import threading
import unittest

from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from chaos.broker_loss import CrashedBrokerLoss
from chaos.coverage import observed
from chaos.isolated_redis import IMAGE, LABEL, IsolatedRedis
from chaos.runner import FaultDriver, Timeline
from chaos.scenarios import generate
from deploy.stack.broker_config import qualification_broker_url
from tests.phase_a.test_qualification_broker import IDENTITY, OP, QUEUE, SCHEMA, broker_run, envelope, loss_fixture


def fixture():
    broker = IsolatedRedis(0)
    broker._id, broker._image, broker.port = "a" * 64, "sha256:" + "b" * 64, 23456
    broker._attempted = True
    ports = {"6379/tcp": [{"HostIp": "127.0.0.1", "HostPort": "23456"}]}
    live = dict(Id=broker._id, Name="/" + broker.name, Image=broker._image, Path="redis-server",
        Created="2026-10-22T12:00:00Z", Config=dict(Labels={LABEL: broker.owner},
        Entrypoint=["redis-server"], Cmd=broker._command(), User="65534:65534"),
        HostConfig=dict(Mounts=[dict(Type="tmpfs", Target="/data")], NetworkMode="bridge",
            PortBindings=ports, ReadonlyRootfs=True, RestartPolicy=dict(Name="no")),
        Mounts=[dict(Type="tmpfs", Destination="/data")], NetworkSettings=dict(Ports=ports),
        State=dict(Running=True, Pid=12345, OOMKilled=False, ExitCode=0, FinishedAt="2026-10-22T12:00:01Z"))
    dead = copy.deepcopy(live); dead["State"].update(Running=False, Pid=0, ExitCode=137)
    server = dict(redis_version="7.4.2", process_id=1, run_id="c" * 40)
    client = Mock()
    client.ping.return_value = True
    client.connection_pool.connection_kwargs = dict(host="127.0.0.1", port=23456, db=2, password=broker._password)
    client.info.side_effect = [server, dict(server, run_id="d" * 40)]
    client.config_get.side_effect = lambda name: {name: "" if name == "save" else "no"}
    client.lrange.return_value = [envelope()]
    client.pipeline.return_value.__enter__ = Mock(return_value=client.pipeline.return_value)
    client.pipeline.return_value.__exit__ = Mock(return_value=False)
    client.pipeline.return_value.execute.return_value = [[], 0]
    events = [dict(Type="container", Action=action, Actor=dict(ID=broker._id, Attributes=attributes), timeNano=ns)
        for action, attributes, ns in (("kill", {"signal": "9"}, 1), ("die", {"exitCode": "137"}, 2))]
    def docker(*args, **kwargs):
        if args[:2] == ("container", "wait"): return "137"
        if args[0] == "events": return "\n".join(json.dumps(e) for e in events)
        return broker._id
    broker._docker = Mock(side_effect=docker)
    broker._inspect = Mock(side_effect=[live, dead, live])
    return broker, client, live, dead, events


def kill_run():
    raw = broker_run()
    raw["observation_finished_monotonic_ns"] = 130
    for row in raw["broker"][2:]: row["monotonic_ns"] += 35
    raw["timeline"] = raw["timeline"][:3]
    facts = dict(source="isolated_broker", container_ref="container", owner="owner", redis_db=2, port=23456)
    server = dict(run_id="old", redis_version="7.4.2", server_pid=1, aof_enabled=False, rdb_enabled=False)
    raw["isolated_broker"] = [dict(facts, **server, event="isolated_redis_started", monotonic_ns=1,
        image=IMAGE, image_id_prefix="image")]
    raw["timeline"].extend([
        dict(facts, **server, event="isolated_redis_queued", queue=QUEUE, messages=[IDENTITY], before_count=1,
             sample_started_ns=30, sample_finished_ns=40, monotonic_ns=41),
        dict(facts, event="isolated_redis_kill_requested", run_id="old", signal=9, daemon_pid=12345, monotonic_ns=42),
        dict(facts, event="isolated_redis_killed", run_id="old", signal=9, wait_exit_code=137, inspect_exit_code=137,
             oom_killed=False, daemon_kill_ns=10, daemon_die_ns=11, monotonic_ns=50),
        dict(facts, **dict(server, run_id="new"), event="isolated_redis_restarted", previous_run_id="old", queue=QUEUE,
             after_count=0, database_size_after=0, sample_started_ns=60, sample_finished_ns=70, monotonic_ns=71)])
    return raw


class PrivateBrokerConfigurationTests(unittest.TestCase):
    def test_default_broker_is_unchanged_when_no_private_credentials_exist(self):
        self.assertIsNone(qualification_broker_url({"SAFEACT_DEMO_SCHEMA": "safeact_demo"}, 1, "safeact-stack"))

    def test_private_credentials_bind_schema_slot_queue_port_and_password(self):
        values = dict(SAFEACT_DEMO_SCHEMA=SCHEMA, SAFEACT_QUAL_REDIS_PORT="23456", SAFEACT_QUAL_REDIS_PASSWORD="a" * 64)
        url = urlsplit(qualification_broker_url(values, 2, QUEUE))
        self.assertEqual((url.hostname, url.port, url.path, len(url.password)), ("127.0.0.1", 23456, "/2", 64))

    def test_demo_shared_database_wrong_slot_and_incomplete_credentials_are_refused(self):
        values = dict(SAFEACT_DEMO_SCHEMA=SCHEMA, SAFEACT_QUAL_REDIS_PORT="23456", SAFEACT_QUAL_REDIS_PASSWORD="a" * 64)
        for changed, db, queue in ((dict(values, SAFEACT_DEMO_SCHEMA="safeact_demo"), 2, QUEUE),
                (values, 1, QUEUE), (values, 3, QUEUE), (values, 2, "safeact-stack"),
                ({"SAFEACT_QUAL_REDIS_PORT": "23456"}, 2, QUEUE)):
            with self.assertRaises(RuntimeError): qualification_broker_url(changed, db, queue)

    def test_malformed_private_endpoints_are_not_accepted(self):
        values = dict(SAFEACT_DEMO_SCHEMA=SCHEMA, SAFEACT_QUAL_REDIS_PORT="23456", SAFEACT_QUAL_REDIS_PASSWORD="a" * 64)
        for key, value in (("SAFEACT_QUAL_REDIS_PORT", "637"), ("SAFEACT_QUAL_REDIS_PORT", "65536"),
                ("SAFEACT_QUAL_REDIS_PASSWORD", "redis://remote")):
            with self.assertRaises(RuntimeError): qualification_broker_url(dict(values, **{key: value}), 2, QUEUE)


class IsolatedBrokerOwnershipTests(unittest.TestCase):
    def test_foreign_container_cannot_be_removed(self):
        broker, client, live, dead, events = fixture()
        live["Config"]["Labels"][LABEL] = "somebody-else"
        broker._docker = Mock(return_value=json.dumps([live]))
        broker._inspect = IsolatedRedis._inspect.__get__(broker)
        with self.assertRaisesRegex(RuntimeError, "ownership"): broker.stop()
        self.assertEqual([c.args[:2] for c in broker._docker.call_args_list], [("container", "inspect")])

    def test_foreign_endpoint_is_rejected_before_any_docker_command(self):
        for key, value in (("host", "remote"), ("port", 6379), ("db", 1), ("password", "foreign")):
            broker, client, live, dead, events = fixture()
            client.connection_pool.connection_kwargs[key] = value
            with self.assertRaises(RuntimeError): broker.crash_recover(client, QUEUE, [envelope()], [IDENTITY], Mock())
            broker._docker.assert_not_called(); broker._inspect.assert_not_called()

    def test_mounts_shared_ports_shell_parents_and_changed_image_block_the_kill(self):
        mutations = [lambda r: r["HostConfig"].update(Binds=["/data:/data"]),
            lambda r: r["Mounts"][0].update(Type="volume"),
            lambda r: r["HostConfig"]["PortBindings"]["6379/tcp"][0].update(HostIp="0.0.0.0"),
            lambda r: r["Config"].update(Entrypoint=["sh"]), lambda r: r.update(Image="foreign"),
            lambda r: r["State"].update(OOMKilled=True), lambda r: r["Config"].update(User="0")]
        for mutate in mutations:
            broker, client, live, dead, events = fixture(); mutate(live)
            with self.assertRaises(RuntimeError): broker.crash_recover(client, QUEUE, [envelope()], [IDENTITY], Mock())
            broker._docker.assert_not_called()

    def test_non_disposable_server_settings_block_the_kill(self):
        for settings in ({"save": "60 1"}, {"appendonly": "yes"}):
            broker, client, live, dead, events = fixture()
            client.config_get.side_effect = lambda name: {name: settings.get(name, "" if name == "save" else "no")}
            with self.assertRaises(RuntimeError): broker.crash_recover(client, QUEUE, [envelope()], [IDENTITY], Mock())
            broker._docker.assert_not_called()

    def test_changed_or_empty_queue_cannot_trigger_a_broker_kill(self):
        for captured in ([], [b"changed"]):
            broker, client, live, dead, events = fixture(); client.lrange.return_value = captured
            with self.assertRaises(RuntimeError): broker.crash_recover(client, QUEUE, [envelope()], [IDENTITY], Mock())
            broker._docker.assert_not_called()

    def test_wait_and_inspect_alone_are_insufficient_without_daemon_events(self):
        broker, client, live, dead, events = fixture(); events.clear()
        with self.assertRaisesRegex(RuntimeError, "daemon"):
            broker.crash_recover(client, QUEUE, [envelope()], [IDENTITY], Mock())
        self.assertNotIn(("container", "start"), [c.args[:2] for c in broker._docker.call_args_list])

    def test_wrong_signal_or_wrong_container_event_blocks_restart(self):
        for mutate in (lambda e: e[0]["Actor"]["Attributes"].update(signal="15"),
                       lambda e: e[1]["Actor"].update(ID="foreign")):
            broker, client, live, dead, events = fixture(); mutate(events)
            with self.assertRaises(RuntimeError): broker.crash_recover(client, QUEUE, [envelope()], [IDENTITY], Mock())

    def test_successful_kill_requires_new_run_id_empty_database_and_all_evidence(self):
        broker, client, live, dead, events = fixture()
        broker.crash_recover(client, QUEUE, [envelope()], [IDENTITY], Mock())
        self.assertEqual([e["event"] for e in broker.events], ["isolated_redis_queued",
            "isolated_redis_kill_requested", "isolated_redis_killed", "isolated_redis_restarted"])
        self.assertNotIn(broker._password, json.dumps(broker.events))
        client.connection_pool.disconnect.assert_called_once()

    def test_persisted_data_or_same_server_after_restart_is_a_failure(self):
        for same, size in ((True, 0), (False, 1)):
            broker, client, live, dead, events = fixture()
            if same:
                client.info.side_effect = [dict(redis_version="7.4.2", process_id=1, run_id="c" * 40)] * 2
            client.pipeline.return_value.execute.return_value = [[], size]
            with self.assertRaises(RuntimeError): broker.crash_recover(client, QUEUE, [envelope()], [IDENTITY], Mock())

    def test_daemon_log_emission_order_is_not_treated_as_kernel_timing(self):
        broker, client, live, dead, events = fixture(); events[0]["timeNano"] = 3
        broker.crash_recover(client, QUEUE, [envelope()], [IDENTITY], Mock())
        self.assertEqual(broker.events[-2]["daemon_kill_ns"], 3)

    def test_cleanup_requires_confirmed_removal_and_is_idempotent(self):
        broker, client, live, dead, events = fixture()
        broker._inspect = Mock(return_value=live)
        broker._docker = Mock(side_effect=[broker._id, ""])
        broker.stop(); broker.stop()
        self.assertTrue(broker._removed)
        self.assertEqual(broker._docker.call_count, 2)

    def test_remaining_container_is_a_cleanup_error(self):
        broker, client, live, dead, events = fixture()
        broker._inspect = Mock(return_value=live); broker._docker = Mock(return_value=broker._id)
        with self.assertRaisesRegex(RuntimeError, "removal"): broker.stop()

    def test_docker_failures_do_not_disclose_arguments_or_credentials(self):
        broker = IsolatedRedis(0)
        with patch("chaos.isolated_redis.subprocess.run", return_value=SimpleNamespace(
                returncode=1, stdout=broker._password, stderr=broker._password)):
            with self.assertRaises(RuntimeError) as caught: broker._docker("container", "create", broker._password)
        self.assertNotIn(broker._password, str(caught.exception))

    def test_remote_docker_context_is_refused_before_image_or_container_commands(self):
        broker = IsolatedRedis(0); broker._docker = Mock(return_value=json.dumps([dict(Endpoints=dict(docker=dict(Host="ssh://remote")))]))
        with patch.dict(os.environ, {"DOCKER_HOST": "unix://local"}), self.assertRaises(RuntimeError): broker.start()
        self.assertEqual(broker._docker.call_count, 1)

    def test_expired_fault_cannot_issue_commands_and_cleanup_budget_is_restored(self):
        broker = IsolatedRedis(0); broker._deadline = 1
        with patch("chaos.isolated_redis.subprocess.run") as run, self.assertRaises(RuntimeError):
            broker._docker("container", "kill", "anything")
        run.assert_not_called()
        broker, client, live, dead, events = fixture()
        client.lrange.return_value = []
        with self.assertRaises(RuntimeError):
            broker.crash_recover(client, QUEUE, [envelope()], [IDENTITY], Mock())
        self.assertIsNone(broker._deadline)

    def test_false_ping_is_bounded_and_cannot_spin_forever(self):
        broker = IsolatedRedis(0); client = Mock(); client.ping.return_value = False
        with patch("chaos.isolated_redis.time.monotonic", side_effect=[0, 9]), self.assertRaises(RuntimeError):
            broker._ready(client)


class IsolatedBrokerCoverageTests(unittest.TestCase):
    def test_kill_restart_and_fresh_delivery_prove_both_loss_and_process_coverage(self):
        self.assertTrue({"broker_messages_lost", "broker_process_killed"} <= observed(kill_run()))

    def test_any_missing_ownership_kill_or_restart_link_removes_credit(self):
        for index in range(3, 7):
            raw = kill_run(); raw["timeline"].pop(index)
            self.assertNotIn("broker_process_killed", observed(raw))
        raw = kill_run(); raw["isolated_broker"] = []
        self.assertNotIn("broker_process_killed", observed(raw))

    def test_oom_wrong_signal_and_persistent_or_nonempty_restart_are_not_credited(self):
        for index, key, value in ((5, "oom_killed", True), (5, "signal", 15), (5, "wait_exit_code", 0),
                (6, "aof_enabled", True), (6, "rdb_enabled", True), (6, "database_size_after", 1),
                (6, "run_id", "old"), (6, "owner", "foreign")):
            raw = kill_run(); raw["timeline"][index][key] = value
            self.assertNotIn("broker_process_killed", observed(raw))

    def test_missing_database_or_fresh_delivery_proof_cannot_be_replaced_by_a_kill_command(self):
        for change in (lambda r: r.update(database=[]), lambda r: r["broker"].pop(),
                lambda r: r.update(observation_finished_monotonic_ns=80)):
            raw = kill_run(); change(raw)
            self.assertNotIn("broker_process_killed", observed(raw))


class IsolatedBrokerDriverTests(unittest.TestCase):
    def test_kill_driver_requires_new_container_and_never_falls_back_to_shared_redis(self):
        old, client, stack, emit = loss_fixture()
        with self.assertRaises(RuntimeError):
            CrashedBrokerLoss(stack, client, 2, QUEUE, lambda: {OP}, Mock(), Mock(), emit)
        stack.kill.assert_not_called()

    def test_independent_pending_sample_precedes_kill_and_cleanup_resumes_relay(self):
        old, client, stack, emit = loss_fixture()
        broker = IsolatedRedis(0); actions = []
        broker.crash_recover = Mock(side_effect=lambda *args: actions.append("kill"))
        loss = CrashedBrokerLoss(stack, client, 2, QUEUE, lambda: {OP}, lambda m: actions.append("db"),
            old.observations, emit, broker=broker)
        with patch.object(loss, "_pause_relay"), patch.object(loss, "resume") as resume:
            loss.finish(threading.Event())
        self.assertEqual(actions, ["db", "kill"])
        self.assertNotIn("flush_queued", client.events)
        resume.assert_called_once(); stack.spawn.assert_called_once_with("worker")

    def test_failed_restart_still_retains_normal_shutdown_path(self):
        old, client, stack, emit = loss_fixture()
        broker = IsolatedRedis(0); broker.crash_recover = Mock(side_effect=RuntimeError("restart failed"))
        loss = CrashedBrokerLoss(stack, client, 2, QUEUE, lambda: {OP}, Mock(), old.observations, emit, broker=broker)
        with patch.object(loss, "_pause_relay"), patch.object(loss, "resume") as resume, self.assertRaises(RuntimeError):
            loss.finish(threading.Event())
        resume.assert_called_once(); stack.spawn.assert_called_once()

    def test_kill_fault_tracks_its_own_required_completion(self):
        arm = SimpleNamespace(begin_broker_loss=Mock(), finish_broker_loss=Mock())
        driver = FaultDriver(SimpleNamespace(mocks={}, proxies={}), Timeline(), arm=arm)
        driver.apply({"kind": "broker_kill"})
        for timer in driver.timers: timer.join(1)
        driver.cancel()
        arm.begin_broker_loss.assert_called_once_with(driver.timeline.add, kill=True)
        self.assertEqual(driver.required, driver.completed)

    def test_development_variants_preserve_first_twelve_and_add_three_private_kills(self):
        first = [generate("dev", "F08", i, 7) for i in range(15)]
        self.assertTrue(all(c.faults[0]["kind"] == "broker_loss" for c in first[:8]))
        self.assertTrue(all(c.faults[0]["kind"] == "broker_redelivery" for c in first[8:12]))
        self.assertEqual({c.connector for c in first[12:]}, {"payments", "crm", "email"})
        self.assertTrue(all(c.faults == ({"kind": "broker_kill"},) for c in first[12:]))

    def test_failed_teardown_marks_cleanup_incomplete_and_prevents_slot_reuse(self):
        from chaos import qualify
        from tests.phase_a.test_chaos_harness import raw_for, scenario
        slot = SimpleNamespace(start=Mock(), stop=Mock(side_effect=RuntimeError("cleanup failed")))
        class Arm:
            name, not_applicable = "safeact", ()
            def setup(self, slot, path): pass
            def teardown(self): pass
        work = queue.Queue()
        for i in range(2): work.put(scenario(scenario_id=f"D-CONTROL-{i:04d}"))
        out = {"raw": io.StringIO(), "results": io.StringIO()}
        with tempfile.TemporaryDirectory() as folder, patch.object(qualify, "make_slot", return_value=slot) as factory, \
                patch.object(qualify, "run", side_effect=lambda slot, arm, case: raw_for(case)):
            qualify.slot_worker(0, [Arm], work, out, threading.Lock(), Path(folder), [])
        row = json.loads(out["results"].getvalue())
        self.assertTrue(row["cleanup_incomplete"])
        self.assertFalse(row["operations"][0]["safe_completion"])
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(work.qsize(), 1)
