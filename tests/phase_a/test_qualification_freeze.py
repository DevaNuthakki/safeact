"""Frozen input and execution-gate checks; no qualification outcomes execute."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chaos import frozen, qualify, scoring
from chaos.baselines.arm import B3Arm
from chaos.baselines.naive_arm import B0Arm
from chaos.baselines.queue_arm import B1Arm, B2Arm
from chaos.qualification_pin import PINNED
from chaos.scenarios import COMBINATIONS, digest, generate


POST_QUALIFICATION = 'docs/phase-e/qual-v1/post-qualification-changes.json'
LIFECYCLE = ('storage/lifecycle_sql/', 'deploy/migrations/', 'core/lifecycle.py',
             'core/provider_keys.py', 'storage/durable_lifecycle_sql.py')

class QualificationFreezeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = frozen.scenarios()

    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root/'chaos').mkdir()
        (root/'chaos/example.py').write_text('value = 1\n')
        (root/'chaos/source.json').write_text(json.dumps(dict(files=[dict(path='chaos/example.py',
            sha256=frozen.sha((root/'chaos/example.py').read_bytes()))])))
        (root/'chaos/cases.jsonl').write_text('\n'.join(case.to_json() for case in self.cases)+'\n')
        manifest = dict(format='safeact-qual-v1', arms=list(frozen.ARMS),
            corpus_digest=digest(self.cases), smoke_ids=frozen.smoke_ids(self.cases),
            inputs={key: dict(path=name, sha256=frozen.sha((root/name).read_bytes()))
                for key, name in (('source', 'chaos/source.json'), ('scenarios', 'chaos/cases.jsonl'))})
        path = root/'manifest.json'
        path.write_text(json.dumps(manifest))
        return root, path, frozen.sha(path.read_bytes())

    def test_frozen_mix_and_smoke_selection_are_disjoint_from_development(self):
        self.assertEqual(len(self.cases), 1200)
        self.assertEqual(sum(case.family == 'CONTROL' for case in self.cases), 60)
        self.assertEqual(sum(case.family in COMBINATIONS for case in self.cases), 120)
        self.assertEqual(len(frozen.smoke_ids(self.cases)), 80)
        self.assertEqual({case.seed for case in self.cases}, {510112026})
        self.assertTrue(all(case.split == 'qual_v1' for case in self.cases))
        for family in COMBINATIONS:
            with self.assertRaises(ValueError):
                generate('dev', family, 0, frozen.SEED)

    def test_input_verification_only_reads_data_and_source(self):
        root, path, pinned = self.fixture()
        with patch('chaos.qualify.make_slot', side_effect=AssertionError('No execution during verification')):
            manifest, cases = frozen.verify(path, root=root, pinned=pinned)
        self.assertEqual(cases, self.cases)
        self.assertEqual(manifest['arms'], list(frozen.ARMS))

    def test_changed_source_extra_source_and_changed_inputs_each_refuse_execution(self):
        for mutation in ('changed', 'extra', 'scenario', 'manifest'):
            root, path, pinned = self.fixture()
            if mutation == 'changed':
                (root/'chaos/example.py').write_text('value = 2\n')
            elif mutation == 'extra':
                (root/'chaos/extra.py').write_text('value = 3\n')
            elif mutation == 'scenario':
                (root/'chaos/cases.jsonl').write_text('changed\n')
            else:
                path.write_text(path.read_text()+' ')
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                frozen.verify(path, root=root, pinned=pinned)

    def test_input_paths_cannot_escape_the_reviewed_repository(self):
        root, path, _ = self.fixture()
        manifest = json.loads(path.read_text())
        manifest['inputs']['source']['path'] = '../source.json'
        path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            frozen.verify(path, root=root, pinned=frozen.sha(path.read_bytes()))

    def test_stop_two_gate_precedes_manifest_and_service_access(self):
        with patch.dict('os.environ', {}, clear=True), patch('chaos.frozen.verify') as verify:
            with self.assertRaisesRegex(SystemExit, 'STOP 2'):
                qualify.main(['--manifest', 'missing.json', '--scope', 'smoke', '--out', 'unused'])
            verify.assert_not_called()

    def test_repository_differs_from_the_qualified_source_only_as_declared(self):
        # The primary run is spent (2026-10-25). Packaging work after it may
        # change the tree, never silently: every difference from the pinned
        # inventory is declared with a step and reason, the frozen inputs
        # and scenarios are unchanged, and the lifecycle is never touched.
        root = frozen.ROOT
        manifest_bytes = (root/'chaos/manifest_qual_v1.json').read_bytes()
        self.assertEqual(frozen.sha(manifest_bytes), PINNED)
        manifest = json.loads(manifest_bytes)
        self.assertEqual(len(manifest['smoke_ids']), 80)
        self.assertIsNone(manifest['calibration']['full_slots'])
        for name, entry in manifest['inputs'].items():
            with self.subTest(input=name):
                self.assertEqual(frozen.sha((root/entry['path']).read_bytes()), entry['sha256'])
        inventory = {entry['path']: entry['sha256'] for entry in json.loads(
            (root/manifest['inputs']['source']['path']).read_text())['files']}
        current = set(frozen.source_paths(root))
        delta = {path: 'changed' for path in inventory.keys() & current
                 if frozen.sha((root/path).read_bytes()) != inventory[path]}
        delta.update({path: 'removed' for path in inventory.keys() - current})
        delta.update({path: 'added' for path in current - inventory.keys()})
        ledger = json.loads((root/POST_QUALIFICATION).read_text())
        self.assertEqual(ledger['manifest_sha256'], PINNED)
        declared = ledger['changes']
        self.assertEqual({change['path']: change['change'] for change in declared}, delta)
        self.assertEqual(len(declared), len(delta))
        for change in declared:
            with self.subTest(path=change['path']):
                self.assertTrue(change['step'].strip() and change['reason'].strip())
                self.assertFalse(change['path'].startswith(LIFECYCLE))
        # Work outside the pinned inventory (the console) is declared too,
        # and must really be outside it.
        for change in ledger.get('outside_pinned_inventory', []):
            with self.subTest(outside=change['path']):
                self.assertTrue(change['path'].startswith('ui/'))
                self.assertEqual(change['kind'], 'UI-only')
                self.assertTrue(change['step'].strip() and change['reason'].strip())
                self.assertFalse(any(path.startswith(change['path']) for path in current | inventory.keys()))
        self.assertEqual(frozen.scenarios(), self.cases)
        if delta:
            # A frozen run from this tree stays refused; a diagnostic of v1
            # needs a worktree at the qualified commit.
            with self.assertRaises(ValueError):
                frozen.verify(root/'chaos/manifest_qual_v1.json')
        else:
            frozen.verify(root/'chaos/manifest_qual_v1.json')

    def test_diagnostic_selection_keeps_primary_safety_failure_authoritative(self):
        root, manifest_path, pinned = self.fixture()
        base = root/'primary'
        base.mkdir()
        case = next(case for case in self.cases if case.family == 'F06')
        row = dict(scenario_id=case.scenario_id, arm='safeact', family='F06', prohibited_duplicates=1)
        raw = dict(row, scenario=json.loads(case.to_json()), started=1, observation_finished=2,
            load=dict(start=dict(load_1=9), end=dict(load_1=2)))
        (base/'environment.json').write_text(json.dumps(dict(scope='full', exit_code=1,
            manifest_sha256=pinned, start=dict(cpu_count=8))))
        (base/'results.jsonl').write_text(json.dumps(row)+'\n')
        (base/'raw.jsonl').write_text(json.dumps(raw)+'\n')
        original = (base/'results.jsonl').read_bytes()
        chosen, provenance = frozen.diagnostic_cases(base, manifest_path, self.cases)
        self.assertEqual(chosen, [case])
        self.assertTrue(provenance['any_primary_safeact_safety_failure'])
        self.assertFalse(provenance['headline_replacement'])
        self.assertEqual((base/'results.jsonl').read_bytes(), original)

    def test_safeact_failure_stops_new_scheduling_but_baseline_failure_does_not(self):
        control = frozen.RunControl(60)
        control.record(dict(arm='b0', prohibited_duplicates=1))
        self.assertTrue(control.allow())
        control.record(dict(arm='safeact', prohibited_duplicates=1))
        self.assertFalse(control.allow())
        self.assertFalse(frozen.RunControl(-1).allow())

    def test_primary_invocation_budget_cannot_be_reclaimed_after_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = dict(budget=dict(execution_ledger_dir='evidence/executions'))
            claim = frozen.claim_invocation(manifest, 'synthetic-manifest', 'full', root/'first', root=root)
            self.assertEqual(json.loads(claim.read_text())['scope'], 'full')
            with self.assertRaisesRegex(SystemExit, 'budget already claimed'):
                frozen.claim_invocation(manifest, 'synthetic-manifest', 'full', root/'second', root=root)
            diagnostic = frozen.claim_invocation(manifest, 'synthetic-manifest', 'diagnostic', root/'rerun', root=root)
            self.assertFalse(json.loads(diagnostic.read_text())['headline_input'])

    def test_budget_stop_preserves_all_requested_denominators_and_feature_na(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = []
            safeact = SimpleNamespace(name='safeact', not_applicable=frozenset())
            frozen.retain_unscheduled([generate('dev', 'F15', 0, 7)], rows, root,
                [safeact, B0Arm, B1Arm, B2Arm, B3Arm])
            self.assertEqual(len(rows), 5)
            by_arm = {row['arm']: row for row in rows}
            self.assertTrue(by_arm['safeact']['harness_errors'])
            self.assertTrue(all(by_arm[arm]['not_applicable'] for arm in ('b0', 'b1', 'b2', 'b3')))
            counts = scoring.aggregate(rows)
            self.assertEqual(counts['applicable_operations'], 1)
            self.assertEqual(counts['not_applicable'], 4)
            self.assertEqual(counts['usefulness']['completed'], 0)

    def test_load_diagnostics_keep_original_failure_and_do_not_classify_unexecuted_rows(self):
        row = dict(arm='safeact', family='F06', prohibited_duplicates=1)
        raw = dict(started=10, observation_finished=22, scenario=dict(settle_seconds=10),
            load=dict(start=dict(load_1=9), end=dict(load_1=2)))
        original = copy.deepcopy(row)
        self.assertEqual(frozen.load_suspect(raw, row, cores=8),
            ['timing_sensitive_family', 'load_above_core_count', 'observation_overrun_above_one_second'])
        self.assertEqual(row, original)
        self.assertEqual(frozen.load_suspect(dict(raw, not_run=True), row, cores=8), [])

    def calibration_fixture(self):
        root, path, pinned = self.fixture()
        manifest_path = root/'chaos/manifest_qual_v1.json'
        manifest_path.write_bytes(path.read_bytes())
        manifest = json.loads(path.read_text())
        manifest['calibration'] = dict(candidates=[1, 2, 4, 6, 7])
        manifest_path.write_text(json.dumps(manifest))
        pinned = frozen.sha(manifest_path.read_bytes())
        records = []
        for slots, load in ((1, 2), (2, 3), (4, 7), (6, 9), (7, 11)):
            base = root/f'calibration-{slots}'
            base.mkdir()
            environment = dict(scope='calibration', slots=slots, manifest_sha256=pinned,
                start=dict(cpu_count=8))
            (base/'environment.json').write_text(json.dumps(environment))
            rows = [dict(scenario_id=sid, arm=arm, load=dict(start=dict(load_1=load), end=dict(load_1=load)),
                harness_errors=['Synthetic failure: outcomes must not select slots'])
                for sid in manifest['smoke_ids'] for arm in frozen.ARMS]
            (base/'raw.jsonl').write_text('\n'.join(json.dumps(row) for row in rows)+'\n')
            records.append(dict(slots=slots,
                environment_path=str((base/'environment.json').relative_to(root)),
                environment_sha256=frozen.sha((base/'environment.json').read_bytes()),
                raw_path=str((base/'raw.jsonl').relative_to(root)),
                raw_sha256=frozen.sha((base/'raw.jsonl').read_bytes())))
        amendment = root/'amendment.json'
        amendment.write_text(json.dumps(dict(manifest_sha256=pinned, candidates=records, selected_slots=4)))
        return root, manifest, amendment

    def test_calibration_uses_load_only_and_retains_every_candidate(self):
        root, manifest, amendment = self.calibration_fixture()
        self.assertEqual(frozen.calibrated_slots(amendment, manifest, root), 4)
        value = json.loads(amendment.read_text())
        value['selected_slots'] = 7
        amendment.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            frozen.calibrated_slots(amendment, manifest, root)
        value['selected_slots'] = 4
        value['candidates'].pop()
        amendment.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            frozen.calibrated_slots(amendment, manifest, root)

    def test_calibration_cannot_use_tampered_or_incomplete_load_records(self):
        for mutation in ('digest', 'missing_end', 'nan', 'scope'):
            root, manifest, amendment = self.calibration_fixture()
            value = json.loads(amendment.read_text())
            record = value['candidates'][0]
            raw_path = root/record['raw_path']
            rows = [json.loads(line) for line in raw_path.read_text().splitlines()]
            if mutation == 'missing_end':
                rows[0]['load'].pop('end')
            elif mutation == 'nan':
                rows[0]['load']['start']['load_1'] = float('nan')
            elif mutation == 'scope':
                env_path = root/record['environment_path']
                env = json.loads(env_path.read_text())
                env['scope'] = 'smoke'
                env_path.write_text(json.dumps(env))
                record['environment_sha256'] = frozen.sha(env_path.read_bytes())
            raw_path.write_text('\n'.join(json.dumps(row) for row in rows)+'\n')
            if mutation != 'digest':
                record['raw_sha256'] = frozen.sha(raw_path.read_bytes())
            else:
                raw_path.write_text(raw_path.read_text()+' ')
            amendment.write_text(json.dumps(value))
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                frozen.calibrated_slots(amendment, manifest, root)


if __name__ == '__main__':
    unittest.main()
