"""Read earlier sealed evidence with current code, without mutating it."""

import argparse
import importlib.util
import json
from pathlib import Path


def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module);return module


def check(root):
    parent=Path(root).parent
    process=parent/'dev-baseline-process-trace-20261023'
    result=load(process/'replay.py','earlier_process_replay').check(process)
    assert result==json.loads((process/'replay.json').read_text())
    observer=parent/'dev-baseline-observer-20261023'
    historical=load(observer/'replay.py','earlier_observer_replay').check(observer)
    assert historical==json.loads((process/'historical-replay.json').read_text())
    return dict(scope='Read-only replay of prior sealed development evidence; no qualification',
        process_cases=len(result['cases']),native=historical['native'],historical=historical['historical'])


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root');p.add_argument('--out');a=p.parse_args();value=check(a.root)
    if a.out:Path(a.out).write_text(json.dumps(value,indent=2)+'\n')
    print('Earlier evidence unchanged:',value['process_cases'],'process cases,',len(value['native']),
          'B3 oracle cases,',sum(r['unchanged_rows'] for r in value['historical']),'SafeAct rows')
