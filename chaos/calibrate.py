"""Verify completed post-review smoke records and write a load-only amendment."""

import argparse
import json
import tempfile
from pathlib import Path

from chaos.frozen import ROOT, calibrated_slots, resolve, sha, verify


def amendment(manifest_path, runs, out, *, root=ROOT):
    manifest, _ = verify(manifest_path, root=root)
    records = []
    for run in runs:
        base = Path(run).resolve()
        names = {name: str((base/name).relative_to(root.resolve()))
                 for name in ('environment.json', 'raw.jsonl')}
        environment_path = resolve(root, names['environment.json'])
        raw_path = resolve(root, names['raw.jsonl'])
        environment = json.loads(environment_path.read_text())
        records.append(dict(slots=environment['slots'],
            environment_path=names['environment.json'], environment_sha256=sha(environment_path.read_bytes()),
            raw_path=names['raw.jsonl'], raw_sha256=sha(raw_path.read_bytes())))
    records.sort(key=lambda record: record['slots'])
    value = dict(manifest_sha256=sha(Path(manifest_path).read_bytes()), candidates=records,
        selected_slots=None, selection_basis='All sampled one-minute loads below CPU count; outcomes unused')
    with tempfile.TemporaryDirectory(prefix='safeact-calibration-verify-') as temporary:
        candidate = Path(temporary)/'candidate.json'
        candidate.write_text(json.dumps(value))
        value['selected_slots'] = calibrated_slots(candidate, manifest, root, select=True)
    path = Path(out)
    with path.open('x') as stream:
        stream.write(json.dumps(value, indent=2)+'\n')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--runs', nargs='+', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    value = amendment(args.manifest, args.runs, args.out)
    print('Verified calibration amendment: '+str(value['selected_slots'])+' slots')


if __name__ == '__main__':
    main()
