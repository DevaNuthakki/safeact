"""The scored snapshot is the one that passed the stability check; a later
change (a post-escalation reopen, any arm) is recorded, never hidden."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chaos import oracle, report, runner, scoring
from chaos.arms.base import Admission, OperationView
from chaos.scenarios import OperationSpec, Scenario
from connectors.ledger import EffectsLedger


def case(settle=3.0):
    return Scenario(scenario_id="D-CONTROL-0000", split="dev", family="CONTROL",
                    connector="payments", contract_version="q-v2", seed=7,
                    operations=(OperationSpec("ch_expected", 100,
                                              acceptable_terminal=("escalated_unresolved",)),),
                    faults=(), completion_achievable=False, settle_seconds=settle)


def arm_with(states, *, name="b3"):
    """observe() answers the given states in order, then repeats the last."""

    class Arm:
        not_applicable = ()
        def __init__(self):
            self.name, self.calls = name, 0
        def prepare(self, scenario):
            pass
        def submit(self, scenario, index, operation, changed=False):
            return Admission("local-1")
        def observe(self, ids):
            state = states[min(self.calls, len(states) - 1)]
            self.calls += 1
            outcome = "pending" if state == "VERIFYING" else "escalated_unresolved"
            return {i: OperationView(i, outcome, state, escalated_at=1.0) for i in ids}
        def quiesce(self):
            pass
        def check_integrity(self):
            return []
    return Arm()


def execute(arm, settle=3.0, stable=0.05):
    with tempfile.TemporaryDirectory() as directory, \
            patch.object(runner, "POLL_SECONDS", 0.01), patch.object(runner, "STABLE_SECONDS", stable):
        slot = SimpleNamespace(index=0, ledger=EffectsLedger(), fault_dir=Path(directory),
                               reset=lambda: None, close_provider_admission=lambda: None,
                               wait_provider_idle=lambda timeout: None)
        return runner.run(slot, arm, case(settle))


class ScoringSnapshotTests(unittest.TestCase):
    def test_transient_reopen_after_stability_is_scored_stable_and_recorded(self):
        arm = arm_with(["ESCALATED_UNRESOLVED"] * 6)
        observe = arm.observe
        def reopen_after_check(ids):
            # With a zero stability window the first poll passes the check, so
            # the second observe is the window close: a reopen there.
            views = observe(ids)
            if arm.calls == 2:
                return {i: OperationView(i, "pending", "VERIFYING", escalated_at=1.0) for i in ids}
            return views
        arm.observe = reopen_after_check
        raw = execute(arm, stable=0)
        self.assertEqual(raw["scored_from"], "stability_check")
        self.assertEqual(raw["operations"]["local-1"]["state"], "ESCALATED_UNRESOLVED")
        [change] = raw["unstable_at_scoring"]
        self.assertEqual(change["scored"]["state"], "ESCALATED_UNRESOLVED")
        self.assertEqual(change["window_close"]["state"], "VERIFYING")
        self.assertEqual(change["final"]["state"], "ESCALATED_UNRESOLVED")
        row = oracle.evaluate(case(), raw)
        self.assertEqual(row["operations"][0]["outcome_class"], "escalated_unresolved")
        self.assertEqual(len(row["unstable_at_scoring"]), 1)
        summary = scoring.summarize([row])
        self.assertEqual(summary["by_arm"]["b3"]["unstable_at_scoring_rows"], 1)
        self.assertIn("| Unstable at scoring |", report.render(summary))
        self.assertIn("Declared trade-off: the attempt budget is fixed.", report.render(summary))
        self.assertIn("(not evaluated)", report.render(summary))

    def test_stable_row_has_no_unstable_record_for_any_arm(self):
        for name in ("safeact", "b0", "b1", "b2", "b3"):
            raw = execute(arm_with(["ESCALATED_UNRESOLVED"], name=name))
            self.assertEqual((raw["scored_from"], raw["unstable_at_scoring"]),
                             ("stability_check", []), name)

    def test_never_stable_row_is_scored_at_window_close_and_later_changes_recorded(self):
        # Still verifying when the window closes, escalated by the final view.
        states = ["VERIFYING"] * 1000
        arm = arm_with(states)
        raw_close = execute(arm, settle=0.2)
        self.assertEqual(raw_close["scored_from"], "window_close")
        self.assertEqual(raw_close["unstable_at_scoring"], [])
        arm = arm_with(states)
        observe = arm.observe
        arm.observe = lambda ids: ({i: OperationView(i, "escalated_unresolved", "ESCALATED_UNRESOLVED")
                                    for i in ids} if arm.quiesced else observe(ids))
        arm.quiesced = False
        arm.quiesce = lambda: setattr(arm, "quiesced", True)
        raw = execute(arm, settle=0.2)
        self.assertEqual(raw["operations"]["local-1"]["state"], "VERIFYING")
        self.assertEqual(raw["unstable_at_scoring"][0]["final"]["state"], "ESCALATED_UNRESOLVED")


if __name__ == "__main__":
    unittest.main()
