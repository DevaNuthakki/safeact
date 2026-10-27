"""Agent runner pieces without a model, a network or a database (M4.5).

Key-file safety (outside the repo, owner-only, never shown), the paid
gate (never in CI), budgets (worst case checked before a call, actual
cost recorded after, cumulative cap), the OpenAI adapter against a fake
client (request shape, no retries, errors without detail), the
deterministic policies, the report, and the results secret scan.
"""

import json
import os
import tempfile
import unittest

from pathlib import Path
from types import SimpleNamespace

from deploy.evaluation.agent_v1 import report as reporting
from deploy.evaluation.agent_v1.secret_scan import scan, scan_text
from mcp_server.agent import models, secrets
from mcp_server.agent.budget import (
    BudgetExhausted, Price, SpendLedger, TrialBudget, TrialLimits,
)
from mcp_server.agent.runner import PROMPT_PATH, REPORT_TOOL


ROOT = Path(__file__).resolve().parents[2]
FAKE_KEY = "sk-test-" + "Q" * 40
PAID = {"SAFEACT_ALLOW_PAID_MODEL": "1"}

# The shared system prompt is versioned with the runner; a change needs a
# new PROMPT_VERSION (and a new frozen manifest before any paid run).
PINNED_PROMPT = {
    "agent-prompt-v1": "54c0c26e92a79fb9237ec0d8e7ff4d31b3b80c410c46b5ade1d3270b5280acea",
}


def key_file(directory, content=FAKE_KEY, mode=0o600):
    path = Path(directory) / "openai.key"
    path.write_text(content + "\n")
    os.chmod(path, mode)
    return path


class KeyFileTests(unittest.TestCase):

    def test_an_owner_only_file_outside_the_repo_is_read_and_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            path = key_file(directory)
            key = secrets.load_openai_key({secrets.KEY_FILE_ENV: str(path)})
            self.assertEqual(key.reveal(), FAKE_KEY)
            for shown in (repr(key), str(key), f"{key}", repr([key])):
                self.assertNotIn(FAKE_KEY, shown)
            with self.assertRaises(TypeError):
                import pickle
                pickle.dumps(key)

    def test_unsafe_key_files_are_refused_without_showing_the_key(self):
        with tempfile.TemporaryDirectory() as directory:
            cases = {
                "group readable": key_file(directory, mode=0o640),
                "missing": Path(directory) / "absent.key",
            }
            inside = ROOT / "deploy" / "evaluation" / "agent_v1" / "__test_key__"
            for label, path in cases.items():
                with self.subTest(case=label):
                    with self.assertRaises(secrets.KeyFileRefused) as refused:
                        secrets.load_openai_key({secrets.KEY_FILE_ENV: str(path)})
                    self.assertNotIn(FAKE_KEY, str(refused.exception))
            link = Path(directory) / "link.key"
            link.symlink_to(key_file(directory, mode=0o600))
            with self.assertRaises(secrets.KeyFileRefused):
                secrets.load_openai_key({secrets.KEY_FILE_ENV: str(link)})
            # A valid owner-only file, but under the repository root.
            valid = key_file(directory, mode=0o600)
            with self.assertRaises(secrets.KeyFileRefused) as refused:
                secrets.load_openai_key(
                    {secrets.KEY_FILE_ENV: str(valid)}, repo_root=directory)
            self.assertIn("outside the repository", str(refused.exception))
            self.assertFalse(inside.exists())

    def test_the_default_path_is_outside_the_repository(self):
        default = secrets.key_file_path({}).resolve()
        self.assertFalse(default.is_relative_to(ROOT))

    def test_paid_calls_need_the_flag_and_never_run_in_ci(self):
        self.assertFalse(secrets.paid_model_allowed({}))
        self.assertTrue(secrets.paid_model_allowed(PAID))
        for ci in ({"CI": "true"}, {"GITHUB_ACTIONS": "true"}):
            with self.subTest(env=ci):
                self.assertFalse(secrets.paid_model_allowed({**PAID, **ci}))
                with self.assertRaises(secrets.PaidModelRefused):
                    models.OpenAIModel("model-x", env={**PAID, **ci},
                                       client=object())


class BudgetTests(unittest.TestCase):

    PRICE = Price(input=1.0, cached_input=0.1, output=4.0)

    def test_cost_from_usage(self):
        self.assertAlmostEqual(self.PRICE.cost(1_000_000, 0), 1.0)
        self.assertAlmostEqual(self.PRICE.cost(1_000_000, 0, 500_000), 0.55)
        self.assertAlmostEqual(self.PRICE.cost(0, 250_000), 1.0)

    def test_a_call_whose_worst_case_crosses_the_trial_cap_is_not_made(self):
        limits = TrialLimits(max_output_tokens=1000, trial_usd=0.005)
        budget = TrialBudget(limits, self.PRICE)
        budget.before_call(["hi"], [])     # worst case ~0.004: allowed
        budget.after_call(models.Usage(500, 900))
        with self.assertRaises(BudgetExhausted) as stop:
            budget.before_call(["hi"], [])
        self.assertEqual(stop.exception.reason, "trial_usd")

    def test_turns_and_tool_calls_are_capped(self):
        budget = TrialBudget(TrialLimits(max_turns=1, max_tool_calls=1))
        budget.before_call([], [])
        budget.after_call(models.Usage())
        with self.assertRaises(BudgetExhausted):
            budget.before_call([], [])
        budget.before_tool_call()
        with self.assertRaises(BudgetExhausted):
            budget.before_tool_call()

    def test_the_total_cap_counts_every_run_in_the_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = SpendLedger(Path(directory) / "spend.jsonl", total_cap_usd=0.01)
            ledger.record(run_id="earlier", trial="x", usd=0.009,
                          input_tokens=1, output_tokens=1)
            budget = TrialBudget(TrialLimits(trial_usd=1.0), self.PRICE, ledger)
            with self.assertRaises(BudgetExhausted) as stop:
                budget.before_call(["x" * 3000], [])
            self.assertEqual(stop.exception.reason, "total_usd")
            budget = TrialBudget(TrialLimits(trial_usd=1.0, max_output_tokens=10),
                                 self.PRICE, ledger)
            budget.before_call(["x"], [])
            budget.after_call(models.Usage(100, 10))
            self.assertAlmostEqual(ledger.spent(), 0.009 + 0.00014, places=6)
            self.assertEqual(len(ledger.path.read_text().splitlines()), 2)


class FakeResponses:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def create(self, **options):
        self.calls.append(options)
        if self.error:
            raise self.error
        return self.response


class OpenAIAdapterTests(unittest.TestCase):

    def model(self, responses):
        client = SimpleNamespace(responses=responses)
        return models.OpenAIModel("snapshot-2026-01-01", env=PAID, client=client)

    def test_request_shape_and_parsing(self):
        response = SimpleNamespace(
            output=[
                SimpleNamespace(type="function_call", call_id="c1",
                                name="get_action_status",
                                arguments='{"operation_id": "x"}'),
                SimpleNamespace(type="message", content=[
                    SimpleNamespace(type="output_text", text="checking")]),
            ],
            usage=SimpleNamespace(
                input_tokens=120, output_tokens=30,
                input_tokens_details=SimpleNamespace(cached_tokens=20)),
        )
        responses = FakeResponses(response)
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c0", "name": "submit_action", "arguments": {"a": 1}}]},
            {"role": "tool", "tool_call_id": "c0", "name": "submit_action",
             "content": '{"is_error": false, "result": {}}'},
        ]
        turn = self.model(responses).respond(
            messages, [{"name": "t", "description": "d", "parameters": {}}], 500)
        options = responses.calls[0]
        self.assertEqual(options["model"], "snapshot-2026-01-01")
        self.assertIs(options["store"], False)
        self.assertIs(options["parallel_tool_calls"], False)
        self.assertEqual(options["max_output_tokens"], 500)
        self.assertNotIn("temperature", options)
        self.assertEqual([i.get("type", i.get("role")) for i in options["input"]],
                         ["system", "user", "function_call", "function_call_output"])
        self.assertEqual(turn.tool_calls[0].arguments, {"operation_id": "x"})
        self.assertEqual(turn.text, "checking")
        self.assertEqual(turn.usage, models.Usage(120, 30, 20))

    def test_errors_carry_no_detail(self):
        class Boom(Exception):
            status_code = 429
        responses = FakeResponses(error=Boom("Incorrect API key sk-leak-" + "x" * 30))
        with self.assertRaises(models.ModelError) as failure:
            self.model(responses).respond([], [], 10)
        self.assertEqual(str(failure.exception), "Boom status=429")

    def test_unparseable_arguments_become_an_explicit_marker(self):
        response = SimpleNamespace(output=[SimpleNamespace(
            type="function_call", call_id="c", name="x", arguments="{bad")],
            usage=None)
        turn = self.model(FakeResponses(response)).respond([], [], 10)
        self.assertEqual(turn.tool_calls[0].arguments, {"_unparseable_arguments": True})


class ObserveModelTests(unittest.TestCase):

    def client(self, models_listed=None, error=None):
        def listing():
            if error:
                raise error
            return iter(models_listed or ())
        return SimpleNamespace(models=SimpleNamespace(list=listing))

    def test_the_listed_shutdown_date_is_recorded(self):
        listed = [SimpleNamespace(id="other", created=1, owned_by="x", shutdown_date=None),
                  SimpleNamespace(id="snap-1", created=2, owned_by="system",
                                  shutdown_date="2027-01-14")]
        observed = models.observe_model("snap-1", env=PAID, client=self.client(listed))
        self.assertEqual(observed["shutdown_date"], "2027-01-14")
        self.assertEqual(observed["source"], "GET /v1/models")

    def test_an_unlisted_snapshot_or_a_failed_listing_refuses(self):
        with self.assertRaises(models.ModelError):
            models.observe_model("snap-9", env=PAID, client=self.client([]))

        class Denied(Exception):
            status_code = 403
        with self.assertRaises(models.ModelError) as failure:
            models.observe_model("snap-1", env=PAID, client=self.client(
                error=Denied("key sk-leak-" + "y" * 30)))
        self.assertEqual(str(failure.exception), "Denied status=403")

    def test_listing_is_gated_like_any_paid_call(self):
        with self.assertRaises(secrets.PaidModelRefused):
            models.observe_model("snap-1", env={**PAID, "CI": "true"},
                                 client=self.client([]))


def tool_result(is_error, result):
    return {"role": "tool", "tool_call_id": "c", "name": "x",
            "content": json.dumps({"is_error": is_error, "result": result})}


class PolicyTests(unittest.TestCase):

    SPEC = models.TaskSpec(action="refund_payment", account="acct",
                           payload={"charge_id": "ch_1", "amount_minor": 5},
                           intent_ref="task-1")

    def drive(self, policy, results):
        model = models.PolicyModel(policy)
        calls, messages = [], []
        for result in [None] + results:
            if result is not None:
                messages.append(tool_result(*result))
            turn = model.respond(messages, [], 10)
            if not turn.tool_calls:
                break
            calls.append(turn.tool_calls[0])
            if turn.tool_calls[0].name == "report_outcome":
                break
        return calls

    def test_the_deterministic_caller_waits_and_never_resubmits(self):
        calls = self.drive(models.guarded_policy(self.SPEC), [
            (False, {"operation_id": "op", "state": "RECORDED", "outcome_known": True}),
            (False, {"operation_id": "op", "state": "UNKNOWN", "outcome_known": False}),
            (False, {"operation_id": "op", "state": "SUCCEEDED", "outcome_known": True}),
        ])
        names = [c.name for c in calls]
        self.assertEqual(names.count("submit_action"), 1)
        self.assertEqual(calls[-1].arguments["status"], "succeeded")

    def test_the_deterministic_caller_reports_refusals_and_escalations(self):
        refused = self.drive(models.guarded_policy(self.SPEC),
                             [(True, {"error": "not_granted"})])
        self.assertEqual(refused[-1].arguments["status"], "refused")
        escalated = self.drive(models.guarded_policy(self.SPEC), [
            (False, {"operation_id": "op", "state": "ESCALATED_UNRESOLVED",
                     "outcome_known": False}),
            (True, {"error": "not_eligible"}),
            (False, {"operation_id": "op", "state": "ESCALATED_UNRESOLVED",
                     "outcome_known": False}),
        ])
        self.assertEqual([c.name for c in escalated],
                         ["submit_action", "request_verification",
                          "get_action_status", "report_outcome"])
        self.assertEqual(escalated[-1].arguments["status"], "unresolved")

    def test_the_adversarial_script_tries_every_forbidden_move(self):
        calls = self.drive(models.adversarial_policy(self.SPEC, "guarded"),
                           [(False, {})] * 4)
        intents = [c.arguments.get("intent_ref") for c in calls if c.name == "submit_action"]
        self.assertEqual(len(set(intents)), 2)
        self.assertIn("approve_resolution", [c.name for c in calls])
        self.assertTrue(any("request_key" in c.arguments for c in calls))


class ReportToolAndPromptTests(unittest.TestCase):

    def test_the_report_tool_is_closed_and_lists_every_status(self):
        from deploy.evaluation.agent_v1.scenarios import REPORTS
        parameters = REPORT_TOOL["parameters"]
        self.assertIs(parameters["additionalProperties"], False)
        self.assertEqual(parameters["properties"]["status"]["enum"], list(REPORTS))

    def test_the_prompt_is_pinned(self):
        import hashlib
        from mcp_server.agent.runner import PROMPT_VERSION
        self.assertEqual(hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest(),
                         PINNED_PROMPT[PROMPT_VERSION])

    def test_the_prompt_never_mentions_a_tool_by_its_arm(self):
        text = PROMPT_PATH.read_text("utf-8")
        for word in ("submit_action", "refund_payment", "SafeAct", "MCP",
                     "guarded", "direct"):
            self.assertNotIn(word, text)


class ReportTests(unittest.TestCase):

    def test_failures_are_listed_and_contributions_computed(self):
        def row(arm, correct, unsafe=0):
            return {"arm": arm, "scenario_id": "S", "family": "f", "trial": 0,
                    "stop_reason": "reported", "trace": [], "score": {
                        "correct_business_outcome": correct,
                        "escalation_justified_expected": False,
                        "justified_escalation": False,
                        "unjustified_escalation": False,
                        "unsafe_attempted": unsafe, "unsafe_executed": unsafe,
                        "unsafe_blocked": 0, "duplicate_effects": unsafe,
                        "unauthorized_effects": 0, "approval_bypass_effects": 0,
                        "unauthorized_approval_attempts": 0, "false_claim": False,
                        "budget_exhausted": False, "tool_calls": 3,
                        "model_turns": 2, "latency_seconds": 1.0,
                        "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001,
                        "report": "succeeded", "effects_on_target": 1 + unsafe,
                        "expected_effects": 1}}
        report = reporting.build(
            [row("guarded_agent", True), row("direct_agent", False, unsafe=1),
             row("deterministic", True)], {"split": "dev"})
        self.assertEqual(len(report["failures"]), 1)
        self.assertEqual(report["contributions"]["guard"]
                         ["unsafe_executed_direct_minus_guarded"], 1)
        self.assertIn("Failures (1)", reporting.markdown(report))


class SecretScanTests(unittest.TestCase):

    def test_each_secret_shape_is_caught(self):
        digest = "ab" * 32
        cases = {
            "openai key": ("x.md", f"key sk-proj-{'A' * 30}\n"),
            "agent key": ("x.jsonl", json.dumps({"note": "sak_" + "c" * 64}) + "\n"),
            "session secret in json": ("x.json", json.dumps({"session": digest})),
            "hex in text": ("x.md", f"cookie {digest}\n"),
        }
        for label, (name, text) in cases.items():
            with self.subTest(case=label):
                self.assertTrue(scan_text(text, suffix=Path(name).suffix))

    def test_words_that_merely_contain_the_prefix_are_not_keys(self):
        for text in ('{"external_ref": "task-8afca4cf-retry-1"}',
                     '{"intent_ref": "ask-me-later-0123456789"}',
                     '{"note": "flask_" "sak_"}'):
            with self.subTest(text=text):
                self.assertEqual(scan_text(text, suffix=".json"), [])
        # A real key after a quote, space or "=" is still caught.
        for text in ('"sk-proj-' + "A" * 30 + '"', "key=sk-" + "B" * 30,
                     " sak_" + "c" * 64):
            with self.subTest(text=text):
                self.assertTrue(scan_text(text, suffix=".md"))

    def test_named_digest_fields_are_allowed(self):
        digest = "cd" * 32
        self.assertEqual(scan_text(json.dumps({"scenario_sha256": digest}),
                                   suffix=".json"), [])
        self.assertEqual(scan_text(f"- prompt_sha256: {digest}\n", suffix=".md"), [])
        # The same value under any other key is still flagged.
        self.assertTrue(scan_text(json.dumps({"scenario_sha256": digest,
                                              "token": "ef" * 32}), suffix=".json"))

    def test_console_logs_are_scanned(self):
        from deploy.evaluation.agent_v1.secret_scan import files_scanned
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "some-run"
            run_dir.mkdir()
            (run_dir / "console.log").write_text(
                '{"level": "INFO", "message": "x", "auth": "sk-proj-' + "D" * 30 + '"}\n')
            self.assertIn(run_dir / "console.log", files_scanned([directory]))
            self.assertIn(str(run_dir / "console.log"), scan([directory]))
        committed = files_scanned([ROOT / "docs" / "phase-d" / "agent-eval-v1"])
        self.assertTrue(any(p.name == "console.log" for p in committed))

    def test_committed_evaluation_files_hold_no_secret_shapes(self):
        paths = [ROOT / "deploy" / "evaluation" / "agent_v1",
                 ROOT / "docs" / "phase-d" / "agent-eval-v1"]
        self.assertEqual(scan([p for p in paths if p.exists()]), {})


if __name__ == "__main__":
    unittest.main()
