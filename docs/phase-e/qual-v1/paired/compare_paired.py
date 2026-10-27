"""Post-hoc paired comparison of SafeAct and B3 on the frozen primary run v1.

    .venv/bin/python docs/phase-e/qual-v1/paired/compare_paired.py           # writes paired.json
    .venv/bin/python docs/phase-e/qual-v1/paired/compare_paired.py --check   # compares only

Reads only the committed scored rows (full/results.jsonl); nothing is re-executed.
Both arms are scored with the frozen rule (chaos.scoring.aggregate, chaos.oracle)
on the same scenarios: the ones applicable to both arms. Achievability is
declared per scenario, so the achievable denominator is the same for both.
Coverage only one arm has (F15 for SafeAct) is reported separately. This
analysis does not replace the headline (924/937 vs 841/874).
"""

import json
import sys

from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from chaos.scoring import aggregate  # noqa: E402

RESULTS = ROOT/'docs/phase-e/qual-v1/full/results.jsonl'
OUT = Path(__file__).resolve().parent/'paired.json'
ARMS = ('safeact', 'b3')
SAFETY = ('safety_failure_rows', 'prohibited_duplicates', 'orphan_effects', 'unaccounted',
          'changed_accepted', 'false_claims', 'compensation_duplicates',
          'invalid_compensation_effects', 'rows_with_errors')


def load(path=RESULTS):
    with open(path) as handle:
        return [json.loads(line) for line in handle]


def measures(rows):
    result = aggregate(rows)
    return dict(scenarios=len(rows), operations=result['applicable_operations'],
                usefulness={key: result['usefulness'][key] for key in ('completed', 'achievable')},
                **{key: result[key] for key in SAFETY})


def analyse(rows):
    by_scenario = defaultdict(dict)
    for row in rows:
        if row['arm'] in ARMS:
            by_scenario[row['scenario_id']][row['arm']] = row
    if any(set(arms) != set(ARMS) for arms in by_scenario.values()):
        raise ValueError('Every scenario needs one SafeAct and one B3 row')
    paired = sorted(sid for sid, arms in by_scenario.items()
                    if not any(arms[arm].get('not_applicable') for arm in ARMS))
    one_arm = sorted(sid for sid in by_scenario if sid not in paired)

    def pick(ids, arm):
        return [by_scenario[sid][arm] for sid in ids]

    families = defaultdict(list)
    for sid in paired:
        families[by_scenario[sid]['safeact']['family']].append(sid)
    by_family = {family: {arm: measures(pick(ids, arm)) for arm in ARMS}
                 for family, ids in sorted(families.items())}
    differing = []
    for sid in paired:
        done = {arm: aggregate([by_scenario[sid][arm]])['usefulness'] for arm in ARMS}
        if any(d['completed'] != d['achievable'] for d in done.values()):
            differing.append(dict(scenario_id=sid, family=by_scenario[sid]['safeact']['family'],
                                  achievable=done['safeact']['achievable'],
                                  **{arm: done[arm]['completed'] for arm in ARMS}))
    coverage = defaultdict(lambda: defaultdict(list))
    for sid in one_arm:
        for arm in ARMS:
            row = by_scenario[sid][arm]
            coverage[row['family']]['runs' if not row.get('not_applicable') else 'not_applicable'].append(arm)
    one_arm_coverage = {}
    for family in sorted(coverage):
        ids = [sid for sid in one_arm if by_scenario[sid]['safeact']['family'] == family]
        runner = sorted(set(coverage[family]['runs']))
        if len(runner) != 1:
            raise ValueError('One-arm coverage must have exactly one running arm per family')
        one_arm_coverage[family] = dict(
            arm=runner[0], reasons=sorted({by_scenario[sid][arm].get('not_applicable_reason')
                                           for sid in ids for arm in ARMS
                                           if by_scenario[sid][arm].get('not_applicable')}),
            **measures(pick(ids, runner[0])))
    unstable = sorted((sid, arm) for sid in paired for arm in ARMS
                      if by_scenario[sid][arm].get('unstable_at_scoring'))
    return dict(
        scope='post-hoc analysis of the frozen primary run v1; committed scored rows, no re-execution; '
              'does not replace the headline',
        source='docs/phase-e/qual-v1/full/results.jsonl',
        rule='chaos.scoring.aggregate on scenarios applicable to both arms',
        paired={arm: measures(pick(paired, arm)) for arm in ARMS},
        by_family=by_family,
        not_achievable_families=sorted(f for f, m in by_family.items()
                                       if m['safeact']['usefulness']['achievable'] == 0),
        differing_scenarios=differing,
        one_arm_coverage=one_arm_coverage,
        unstable_at_scoring={'rows': [list(item) for item in unstable],
                             'in_differing_scenarios': sum(1 for sid, _ in unstable
                                                           if sid in {d['scenario_id'] for d in differing})})


def main(argv):
    result = analyse(load())
    text = json.dumps(result, indent=1, sort_keys=True) + '\n'
    if '--check' in argv:
        if OUT.read_text() != text:
            sys.exit('paired.json differs from a recomputation')
        print('paired.json matches')
        return
    OUT.write_text(text)
    for arm, m in result['paired'].items():
        print(arm, m)


if __name__ == '__main__':
    main(sys.argv[1:])
