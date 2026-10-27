"""Frozen runs keep the machine awake themselves and refuse battery power."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chaos import environment, frozen


def supplies(**entries):
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    for name, (kind, status) in entries.items():
        (root/name).mkdir()
        (root/name/'type').write_text(kind + '\n')
        if status:
            (root/name/'status').write_text(status + '\n')
    return temporary, root


class PowerSourceTests(unittest.TestCase):
    def test_macos_reads_pmset(self):
        with patch.object(sys, 'platform', 'darwin'):
            for text, expected in (("Now drawing from 'AC Power'", 'ac'),
                                   ("Now drawing from 'Battery Power'", 'battery'),
                                   ('', None)):
                with patch.object(environment, '_run', return_value=text):
                    self.assertEqual(environment.power_source(), expected)

    def test_linux_reads_power_supply_class(self):
        with patch.object(sys, 'platform', 'linux'):
            for entries, expected in ((dict(BAT0=('Battery', 'Discharging'), AC=('Mains', None)), 'battery'),
                                      (dict(BAT0=('Battery', 'Charging')), 'ac'),
                                      (dict(AC=('Mains', None)), 'no_battery'),
                                      ({}, 'no_battery')):
                temporary, root = supplies(**entries)
                with temporary:
                    self.assertEqual(environment.power_source(root), expected, entries)
            self.assertEqual(environment.power_source(Path('/nonexistent/power')), 'no_battery')


class PrepareMachineTests(unittest.TestCase):
    def test_battery_and_unknown_power_refuse(self):
        for power in ('battery', None):
            with patch.object(environment, 'power_source', return_value=power), \
                    patch.object(environment, 'hold_awake', side_effect=AssertionError('not reached')):
                with self.assertRaisesRegex(SystemExit, 'battery or unknown power'):
                    frozen.prepare_machine()

    def test_missing_assertion_refuses(self):
        with patch.object(environment, 'power_source', return_value='ac'), \
                patch.object(environment, 'hold_awake', side_effect=RuntimeError('no assertion')):
            with self.assertRaisesRegex(SystemExit, 'own caffeinate'):
                frozen.prepare_machine()

    def test_non_macos_records_not_applicable(self):
        with patch.object(sys, 'platform', 'linux'):
            record, process = environment.hold_awake(os.getpid())
        self.assertEqual((record['applicable'], process), (False, None))
        self.assertEqual(environment.awake_status(None), dict(applicable=False))

    def test_refusal_happens_before_output_or_invocation_claim(self):
        args = SimpleNamespace(families=None, cases=None, limit=None, corpus='dev', arms='safeact',
                               scope='smoke', manifest='m.json', slots=1, calibration=None,
                               diagnostics_from=None, out='unused-out')
        manifest = dict(calibration=dict(candidates=[1]), smoke_ids=[])
        with patch.dict(os.environ, SAFEACT_RUN_FROZEN_QUALIFICATION='1'), \
                patch.object(frozen, 'verify', return_value=(manifest, [])), \
                patch.object(environment, 'power_source', return_value='battery'), \
                patch.object(frozen, 'claim_invocation', side_effect=AssertionError('budget claimed')), \
                patch.object(Path, 'mkdir', side_effect=AssertionError('output created')):
            with self.assertRaisesRegex(SystemExit, 'battery'):
                frozen.execute(args)


@unittest.skipUnless(sys.platform == 'darwin', 'caffeinate is macOS only')
class CaffeinateTests(unittest.TestCase):
    def test_own_caffeinate_asserts_and_is_recorded(self):
        record, process = environment.hold_awake(os.getpid())
        try:
            self.assertTrue(record['assertion_observed'])
            self.assertEqual(record['command'], ['caffeinate', '-i', '-w', str(os.getpid())])
            status = environment.awake_status(process)
            self.assertTrue(status['running'] and status['assertion_observed'])
        finally:
            process.terminate()
            process.wait()
        self.assertFalse(environment.awake_status(process)['running'])


if __name__ == '__main__':
    unittest.main()
