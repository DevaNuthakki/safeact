"""Replay common-oracle results and saved SafeAct results without services."""

import argparse
import hashlib
import json
from pathlib import Path

from chaos import coverage, oracle
from chaos.scenarios import Scenario


def check(root):
    root=Path(root);native=[];historical=[]
    for path in sorted((root/'raw').glob('test_*.json')):
        value=json.loads(path.read_text());raw=value['raw']
        scenario=Scenario.from_json(json.dumps(raw['scenario']))
        assert json.loads(json.dumps(oracle.evaluate(scenario,raw)))==value['result'],path
        assert value['passed'] and value['cleanup_complete'] and not value['failures'] and not value['skipped'],path
        assert not coverage.observed(raw),('Unexpected process interleaving credit',path)
        native.append(dict(case=value['case'],result_sha256=hashlib.sha256(json.dumps(value['result'],sort_keys=True).encode()).hexdigest()))
    assert len(native)==5,len(native)
    for packet in ('dev-workflows-20261022','dev-boundaries-20261022'):
        base=root.parent/packet
        raw=[json.loads(line) for line in (base/'raw.jsonl').read_text().splitlines()]
        expected=[json.loads(line) for line in (base/'results.jsonl').read_text().splitlines()]
        by_id={(r['scenario_id'],r['arm']):r for r in expected}
        for row in raw:
            result=json.loads(json.dumps(oracle.evaluate(Scenario.from_json(json.dumps(row['scenario'])),row)))
            saved=by_id[row['scenario_id'],row['arm']]
            # Qualification adds coverage and scenario metadata after scoring.
            assert all(saved[k]==v for k,v in result.items()),(packet,row['scenario_id'])
        historical.append(dict(packet=packet,unchanged_rows=len(raw)))
    return dict(native=native,historical=historical,scope='Read-only development replay; no qualification run')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root');p.add_argument('--out');a=p.parse_args()
    value=check(a.root)
    if a.out:Path(a.out).write_text(json.dumps(value,indent=2)+'\n')
    print(json.dumps(value))
