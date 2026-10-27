"""The post-hoc paired comparison is reproducible from the committed scored rows."""

import importlib.util
import json
import unittest

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT/'docs/phase-e/qual-v1/paired/compare_paired.py'


def module():
    spec = importlib.util.spec_from_file_location('compare_paired', SCRIPT)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


class PairedAnalysisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paired = module()
        cls.result = cls.paired.analyse(cls.paired.load())

    def test_committed_json_equals_a_recomputation(self):
        committed = json.loads((SCRIPT.parent/'paired.json').read_text())
        self.assertEqual(committed, json.loads(json.dumps(self.result)))

    def test_same_denominator_and_equal_safety(self):
        safeact, b3 = self.result['paired']['safeact'], self.result['paired']['b3']
        self.assertEqual(safeact['scenarios'], b3['scenarios'])
        self.assertEqual(safeact['usefulness']['achievable'], b3['usefulness']['achievable'])
        for key in self.paired.SAFETY:
            self.assertEqual(safeact[key], 0, key)
            self.assertEqual(b3[key], 0, key)

    def test_paired_plus_one_arm_coverage_is_the_headline(self):
        summary = json.loads((ROOT/'docs/phase-e/qual-v1/full/summary.json').read_text())
        for arm in ('safeact', 'b3'):
            extra = [m['usefulness'] for m in self.result['one_arm_coverage'].values() if m['arm'] == arm]
            headline = summary['by_arm'][arm]['usefulness']
            for key in ('completed', 'achievable'):
                self.assertEqual(self.result['paired'][arm]['usefulness'][key]
                                 + sum(u[key] for u in extra), headline[key])


if __name__ == '__main__':
    unittest.main()
