"""Connector port, lookup normalization, payments adapter and mock.

No database. Effects are counted from the harness ledger only.
"""

import ast
import threading
import time

from dataclasses import replace
import unittest

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from connectors.base import (
    DispatchKind,
    DispatchResult,
    LookupResult,
    LookupTarget,
    RawLookup,
    normalize_lookup,
)
from connectors.crm import CrmConnector
from connectors.crm_mock import MockCrmProvider
from connectors.email import EmailConnector
from connectors.email_mock import MockEmailProvider
from connectors.ledger import EffectsLedger
from connectors.payments import PaymentsConnector
from connectors.payments_mock import MockPaymentsProvider
from core.connector_profiles import (
    crm_profile,
    email_profile,
    payments_profile,
)
from core.contracts import (
    EvidenceKind as E,
    LookupSemantics,
    canonical_payload_bytes,
    canonical_payload_hash,
)
from storage.durable_lifecycle import DispatchIntent


ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 18, tzinfo=timezone.utc)
PARAMS = {"charge_id": "ch_1", "amount_minor": 1200}


class FakeClock:

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def intent(key="pk-1", params=PARAMS, action="refund_payment"):
    wire = canonical_payload_bytes(params).decode()
    return DispatchIntent(
        tenant_id="t", action_type=action, resource_id="ch_1",
        provider_account_id="acct", connector_id="mock-payments",
        contract_version="v1", payload_sha256=canonical_payload_hash(params),
        payload_canonical=wire, provider_idempotency_key=key,
    )


def target(key="pk-1", params=PARAMS, *, after=timedelta(minutes=5)):
    return LookupTarget(
        operation_id=uuid4(), provider_account_id="acct",
        provider_idempotency_key=key,
        payload_sha256=canonical_payload_hash(params),
        first_dispatch_at=T0, observed_at=T0 + after,
    )


class PortTypeTests(unittest.TestCase):

    def test_only_decisive_results_carry_references(self):
        for kind, reference, ok in (
            (DispatchKind.APPLIED, "re_1", True),
            (DispatchKind.APPLIED, None, False),
            (DispatchKind.NOT_APPLIED, "rej", True),
            (DispatchKind.AMBIGUOUS, None, True),
            (DispatchKind.AMBIGUOUS, "re_1", False),
        ):
            with self.subTest(kind=kind, reference=reference):
                if ok:
                    DispatchResult(kind, "r", reference)
                else:
                    with self.assertRaises(ValueError):
                        DispatchResult(kind, "r", reference)

    def test_ambiguous_results_cannot_observe_decisive_evidence(self):
        with self.assertRaises(ValueError):
            DispatchResult(DispatchKind.AMBIGUOUS, "r", observed=E.APPLIED)
        with self.assertRaises(ValueError):
            DispatchResult(DispatchKind.AMBIGUOUS, "r", retryable=True)
        with self.assertRaises(ValueError):
            LookupResult(E.NOT_FOUND, "ref")
        with self.assertRaises(ValueError):
            LookupResult(E.APPLIED)


class NormalizationTests(unittest.TestCase):

    def normalize(self, profile, raw, **options):
        return normalize_lookup(profile, target(**options), raw)

    def test_strong_absence_inside_retention_is_proof(self):
        found = self.normalize(
            payments_profile(), RawLookup("absent", "pk-1")
        )
        self.assertEqual(found.kind, E.PROVEN_NOT_APPLIED)
        self.assertEqual(found.reference, "lookup-absent:pk-1")

    def test_strong_absence_after_key_retention_is_not_proof(self):
        profile = payments_profile()
        # Clearly before and clearly after the retention-minus-margin
        # cutoff (23 h): never at the edge.
        before = self.normalize(
            profile, RawLookup("absent", "pk-1"), after=timedelta(hours=22)
        )
        after = self.normalize(
            profile, RawLookup("absent", "pk-1"), after=timedelta(hours=24)
        )
        self.assertEqual(before.kind, E.PROVEN_NOT_APPLIED)
        self.assertEqual(after.kind, E.NOT_FOUND)
        self.assertEqual(after.note, "absent_after_key_retention")

    def test_eventual_absence_is_never_proof(self):
        for profile in (
            payments_profile(lookup=LookupSemantics.EVENTUAL), crm_profile()
        ):
            with self.subTest(profile=profile.connector_id):
                self.assertEqual(
                    self.normalize(profile, RawLookup("absent", "x")).kind,
                    E.NOT_FOUND,
                )

    def test_no_lookup_contract_is_always_inconclusive(self):
        for status in ("found", "absent", "in_progress"):
            with self.subTest(status=status):
                result = self.normalize(
                    email_profile(), RawLookup(status, "x", "ref")
                )
                self.assertEqual(result.kind, E.INCONCLUSIVE)

    def test_found_record_must_bind_to_this_payload(self):
        profile = payments_profile()
        good = RawLookup("found", "pk-1", "re_1", canonical_payload_hash(PARAMS))
        other = RawLookup(
            "found", "pk-1", "re_1",
            canonical_payload_hash({**PARAMS, "amount_minor": 9999}),
        )
        self.assertEqual(self.normalize(profile, good).kind, E.APPLIED)
        lie = self.normalize(profile, other)
        self.assertEqual(lie.kind, E.INCONCLUSIVE)
        self.assertEqual(lie.note, "out_of_contract_binding_mismatch")

    def test_in_progress_unavailable_and_unknown_status_are_inconclusive(self):
        for status in ("in_progress", "unavailable", "teapot"):
            with self.subTest(status=status):
                self.assertEqual(self.normalize(
                    payments_profile(), RawLookup(status, "pk-1")
                ).kind, E.INCONCLUSIVE)


class PaymentsAdapterTests(unittest.TestCase):

    def setUp(self):
        self.ledger = EffectsLedger()
        self.clock = FakeClock()
        self.provider = MockPaymentsProvider(
            ledger=self.ledger, retention_seconds=100, clock=self.clock,
        )
        self.connector = PaymentsConnector(self.provider)

    def send(self, key="pk-1", params=PARAMS, timeout=None):
        return self.connector.dispatch(
            intent(key, params), operation_id=uuid4(), timeout=timeout,
        )

    def effects(self, key="pk-1"):
        return len(self.ledger.effects_for(key))

    def test_classification_of_every_provider_answer(self):
        cases = (
            ("fail_before_effect", True, DispatchKind.AMBIGUOUS,
             "ambiguous_no_response", 0),
            ("fail_after_effect", True, DispatchKind.AMBIGUOUS,
             "ambiguous_no_response", 1),
            ("server_error_after_effect", True, DispatchKind.AMBIGUOUS,
             "ambiguous_error_after_send", 1),
            ("reject_next", "rej_1", DispatchKind.NOT_APPLIED,
             "provider_rejected", 0),
        )
        for index, (switch, value, kind, reason, effects) in enumerate(cases):
            key = f"pk-{index}"
            with self.subTest(switch=switch):
                setattr(self.provider, switch, value)
                result = self.send(key)
                self.assertEqual((result.kind, result.reason), (kind, reason))
                self.assertEqual(self.effects(key), effects)

        applied = self.send("pk-ok")
        self.assertEqual(applied.kind, DispatchKind.APPLIED)
        self.assertEqual(
            applied.reference, self.ledger.effects_for("pk-ok")[0]
        )

    def test_same_key_replays_and_changed_parameters_conflict(self):
        first = self.send()
        again = self.send()
        self.assertEqual(again.reference, first.reference)
        changed = self.send(params={**PARAMS, "amount_minor": 1})
        self.assertEqual(changed.reason, "ambiguous_key_conflict")
        self.assertEqual(changed.observed, E.INCONCLUSIVE)
        self.assertEqual(self.effects(), 1)

    def test_key_retention_boundary(self):
        self.send()
        # Clearly inside retention: replay is absorbed.
        self.clock.now += 50
        self.send()
        self.assertEqual(self.effects(), 1)
        # Clearly past retention: the provider forgot the key, a
        # replay applies a second effect, and lookup sees nothing.
        self.clock.now += 100
        self.assertEqual(self.provider.lookup("pk-1").status, "absent")
        self.send()
        self.assertEqual(self.effects(), 2)

    def test_in_flight_same_key_is_refused_without_effect(self):
        held = self.provider.hold_next()
        results = []
        first = threading.Thread(
            target=lambda: results.append(self.send())
        )
        first.start()
        deadline = time.monotonic() + 5
        while self.provider.lookup("pk-1").status != "in_progress":
            self.assertLess(time.monotonic(), deadline)

        second = self.send()
        self.assertEqual(second.reason, "provider_in_flight_conflict")
        self.assertEqual(self.effects(), 0)

        held.release()
        first.join(5)
        self.assertEqual(results[0].kind, DispatchKind.APPLIED)
        self.assertEqual(self.effects(), 1)

    def test_caller_timeout_then_late_commit(self):
        held = self.provider.hold_next()
        result = self.send(timeout=0)
        self.assertEqual(result.reason, "ambiguous_no_response")
        self.assertEqual(self.provider.lookup("pk-1").status, "in_progress")
        self.assertEqual(self.effects(), 0)

        held.release()
        self.assertTrue(held.wait_committed())
        self.assertEqual(self.effects(), 1)
        self.assertEqual(self.provider.lookup("pk-1").status, "found")

    def test_lookup_through_adapter(self):
        self.send()
        found = self.connector.lookup(target(), payments_profile())
        self.assertEqual(found.kind, E.APPLIED)
        self.assertEqual(found.reference, self.ledger.effects_for("pk-1")[0])

        self.provider.lookup_unavailable = True
        self.assertEqual(
            self.connector.lookup(target(), payments_profile()).kind,
            E.INCONCLUSIVE,
        )

    def test_eventual_mock_hides_new_effects_until_revealed(self):
        provider = MockPaymentsProvider(
            ledger=self.ledger, lookup_semantics=LookupSemantics.EVENTUAL
        )
        provider.hide_new_effects = True
        PaymentsConnector(provider).dispatch(
            intent("pk-e"), operation_id=uuid4()
        )
        self.assertEqual(provider.lookup("pk-e").status, "absent")
        provider.reveal_hidden_effects()
        self.assertEqual(provider.lookup("pk-e").status, "found")

    def test_adapter_accepts_only_its_actions_with_a_key(self):
        self.assertTrue(self.connector.accepts(intent()))
        self.assertFalse(self.connector.accepts(intent(key=None)))
        self.assertFalse(self.connector.accepts(intent(action="send")))


class CrmAndEmailAdapterTests(unittest.TestCase):

    def setUp(self):
        self.ledger = EffectsLedger()
        self.crm = MockCrmProvider(ledger=self.ledger)
        self.email = MockEmailProvider(ledger=self.ledger)

    def crm_intent(self, params=PARAMS):
        return replace(
            intent(key=None, params=params,
                   action="create_replacement_order"),
            connector_id="mock-crm",
        )

    def test_crm_has_no_dedup_so_repeats_duplicate(self):
        connector = CrmConnector(self.crm)
        operation_id = uuid4()
        for _ in range(2):
            connector.dispatch(self.crm_intent(), operation_id=operation_id)
        # Why ambiguous CRM work must never be blindly repeated.
        self.assertEqual(len(self.ledger.effects_for(str(operation_id))), 2)

    def test_crm_search_lag_lost_and_corrupt_records(self):
        connector = CrmConnector(self.crm)
        profile = crm_profile()

        def look(operation_id):
            return connector.lookup(
                replace(target(), operation_id=operation_id), profile
            )

        lagging, lost, corrupt = uuid4(), uuid4(), uuid4()
        self.crm.index_lag = True
        connector.dispatch(self.crm_intent(), operation_id=lagging)
        self.crm.index_lag = False
        self.assertEqual(look(lagging).kind, E.NOT_FOUND)
        self.crm.reindex()
        self.assertEqual(look(lagging).kind, E.APPLIED)

        self.crm.drop_from_index = True
        connector.dispatch(self.crm_intent(), operation_id=lost)
        self.crm.reindex()
        self.assertEqual(look(lost).kind, E.NOT_FOUND)

        connector.dispatch(self.crm_intent(), operation_id=corrupt)
        self.crm.corrupt_search = True
        lie = look(corrupt)
        self.assertEqual(lie.kind, E.INCONCLUSIVE)
        self.assertEqual(lie.note, "out_of_contract_binding_mismatch")

    def test_crm_and_email_classification(self):
        cases = (
            (CrmConnector(self.crm), self.crm, self.crm_intent()),
            (EmailConnector(self.email), self.email, replace(
                intent(key=None, action="send_refund_confirmation"),
                connector_id="mock-email",
            )),
        )
        for connector, provider, sent in cases:
            with self.subTest(connector=connector.connector_id):
                self.assertTrue(connector.accepts(sent))
                provider.reject_next = "rej"
                self.assertEqual(
                    connector.dispatch(sent, operation_id=uuid4()).kind,
                    DispatchKind.NOT_APPLIED,
                )
                for switch, reason in (
                    ("fail_before_effect", "ambiguous_no_response"),
                    ("fail_after_effect", "ambiguous_no_response"),
                    ("server_error_after_effect",
                     "ambiguous_error_after_send"),
                ):
                    setattr(provider, switch, True)
                    result = connector.dispatch(sent, operation_id=uuid4())
                    self.assertEqual(
                        (result.kind, result.reason),
                        (DispatchKind.AMBIGUOUS, reason),
                    )
                self.assertEqual(
                    connector.dispatch(sent, operation_id=uuid4()).kind,
                    DispatchKind.APPLIED,
                )

    def test_email_lookup_is_never_attempted(self):
        result = EmailConnector(self.email).lookup(target(), email_profile())
        self.assertEqual(
            (result.kind, result.note), (E.INCONCLUSIVE, "no_lookup")
        )


# Connector logic and the execution path: none of them may reach the
# harness ledger, so effects can only be counted provider-side.
LEDGER_FREE_MODULES = (
    "connectors/base.py",
    "connectors/payments.py",
    "connectors/crm.py",
    "connectors/email.py",
    "storage/experimental_durable_execution.py",
    "storage/experimental_recovery_worker.py",
    # The guarded MCP boundary sees only what SafeAct records.
    "mcp_server/server.py",
    "mcp_server/tools.py",
    "mcp_server/untrusted.py",
    "mcp_server/settings.py",
    "storage/agent_api.py",
    # HTTP path (M5.2): the client facades, the provider wire APIs (they
    # answer from the mocks' own state) and the stack processes.
    "connectors/http_providers.py",
    "connectors/stripe_payments.py",
    "connectors/http_mock/provider_api.py",
    "deploy/stack/config.py",
    "deploy/stack/worker.py",
    "deploy/stack/loops.py",
)
# Harness-only admin API paths. Nothing on the connector side names one.
ADMIN_PATHS = ("/ledger", "/faults", "/holds", "/reveal", "/reindex")
LEDGER_NAMES = {
    "EffectsLedger", "snapshot", "effects_for", "calls_for",
    "call_times", "_ledger", "ledger",
}


# Modules allowed to read provider_facts: the port, the adapters that set
# them, and the two places that write them into attempt evidence.
FACT_MODULES = {
    "connectors/base.py",
    "connectors/stripe_payments.py",
    "storage/experimental_durable_execution.py",
    "storage/experimental_recovery_worker.py",
}


class ProviderFactsTests(unittest.TestCase):

    def test_facts_are_a_few_short_strings(self):
        from connectors.base import DispatchKind, DispatchResult

        ok = DispatchResult(DispatchKind.APPLIED, "r", "re_1",
                            provider_facts={"refund_status": "pending"})
        self.assertEqual(ok.provider_facts, (("refund_status", "pending"),))
        for bad in ({"Bad-Key": "x"}, {"k": 1}, {"k": "x" * 65},
                    {f"k{i}": "v" for i in range(9)}):
            with self.subTest(facts=bad):
                with self.assertRaises(ValueError):
                    DispatchResult(DispatchKind.APPLIED, "r", "re_1",
                                   provider_facts=bad)

    def test_no_decision_reads_provider_facts(self):
        for path in sorted(
            p for folder in ("core", "storage", "api", "mcp_server",
                             "connectors", "deploy")
            for p in (ROOT / folder).rglob("*.py")
        ):
            relative = path.relative_to(ROOT).as_posix()
            if relative in FACT_MODULES:
                continue
            tree = ast.parse(path.read_text("utf-8"))
            with self.subTest(module=relative):
                self.assertFalse([
                    node for node in ast.walk(tree)
                    if isinstance(node, ast.Attribute)
                    and node.attr == "provider_facts"
                ])

    def test_the_reconciler_only_records_facts(self):
        tree = ast.parse((ROOT / "storage/experimental_recovery_worker.py")
                         .read_text("utf-8"))
        users = [
            function.name for function in ast.walk(tree)
            if isinstance(function, ast.FunctionDef)
            and any(isinstance(n, ast.Attribute) and n.attr == "provider_facts"
                    for n in ast.walk(function))
        ]
        self.assertEqual(users, ["_observe"])


class LedgerIsolationTests(unittest.TestCase):

    def test_connector_and_execution_code_never_touch_the_ledger(self):
        for relative in LEDGER_FREE_MODULES:
            tree = ast.parse((ROOT / relative).read_text("utf-8"))
            with self.subTest(module=relative):
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        modules = [a.name for a in node.names]
                        if isinstance(node, ast.ImportFrom):
                            modules.append(node.module or "")
                        self.assertFalse(
                            any("ledger" in m for m in modules), modules
                        )
                    if isinstance(node, ast.Attribute):
                        self.assertNotIn(node.attr, LEDGER_NAMES)
                    if isinstance(node, ast.Name):
                        self.assertNotIn(node.id, LEDGER_NAMES)

    def test_connector_side_never_names_an_admin_path(self):
        for relative in LEDGER_FREE_MODULES:
            tree = ast.parse((ROOT / relative).read_text("utf-8"))
            with self.subTest(module=relative):
                for node in ast.walk(tree):
                    if isinstance(node, ast.Constant) and isinstance(
                        node.value, str
                    ):
                        self.assertFalse(
                            any(path in node.value for path in ADMIN_PATHS),
                            node.value[:60],
                        )

    def test_mock_lookups_never_read_the_ledger(self):
        for module, function in (
            ("connectors/payments_mock.py", "lookup"),
            ("connectors/crm_mock.py", "search"),
        ):
            tree = ast.parse((ROOT / module).read_text("utf-8"))
            lookup = next(
                node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef)
                and node.name == function
            )
            names = {
                node.attr for node in ast.walk(lookup)
                if isinstance(node, ast.Attribute)
            }
            with self.subTest(module=module):
                self.assertNotIn("_ledger", names)

    def test_ledger_reads_are_not_part_of_the_provider_surface(self):
        provider = MockPaymentsProvider(ledger=EffectsLedger())
        for name in ("snapshot", "effects_for", "calls_for"):
            self.assertFalse(hasattr(provider, name))


if __name__ == "__main__":
    unittest.main()
