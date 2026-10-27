"""Frozen input verification and explicitly gated mock-only comparison."""

import hashlib
import json
import math
import os
import queue
import tempfile
import threading
import time
from pathlib import Path

from chaos import SLOT_REDIS_DBS, coverage, environment, oracle, report, scoring
from chaos.applicability import critical_labels
from chaos.fault_contracts import validate_scenario
from chaos.scenarios import COMBINATIONS, FAMILIES, Scenario, corpus, digest

ROOT = Path(__file__).resolve().parents[1]
SEED = 510112026
COUNTS = {'CONTROL': 60, **{family: 64 if index < 12 else 63
    for index, family in enumerate(FAMILIES[1:])}, **{family: 20 for family in COMBINATIONS}}
ARMS = ('safeact', 'b0', 'b1', 'b2', 'b3')
TIMING_SENSITIVE = frozenset({'F05', 'F06', 'F07', 'F08', 'F11', 'F16'})
SOURCE_ROOTS = ('.github', 'api', 'chaos', 'connectors', 'core', 'deploy', 'mcp_server', 'storage')
SOURCE_SUFFIXES = {'.py', '.sql', '.yaml', '.yml', '.toml', '.lock', '.txt'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def source_paths(root=ROOT):
    files = {str(path.relative_to(root)) for name in SOURCE_ROOTS
        for path in (root/name).rglob('*') if path.is_file() and path.suffix in SOURCE_SUFFIXES
        and '__pycache__' not in path.parts and '.venv' not in path.parts}
    files.update(str(path.relative_to(root)) for pattern in ('requirements*.lock', 'pyproject.toml')
        for path in root.glob(pattern) if path.is_file())
    # Generated data and the digest anchor cannot hash themselves recursively.
    return sorted(path for path in files if path != 'chaos/qualification_pin.py')


def scenarios():
    result = corpus('qual_v1', COUNTS, SEED)
    if len(result) != 1200 or len({s.scenario_id for s in result}) != 1200:
        raise ValueError('Invalid qualification construction')
    for case in result:
        validate_scenario(case)
    return result


def smoke_ids(cases):
    chosen = []
    # Each email slot family keeps one email cell in the sample.
    special = {'CONTROL': [0, 1, 2], 'F05': [0, 1, 2, 5], 'F06': [0, 1, 2, 4],
               'F07': [0, 1, 3, 4], 'F08': [0, 6, 8, 12], 'F13': [0, 1, 2, 3, 4],
               'F16': [0, 3, 4, 5]}
    for family in COUNTS:
        mine = [s for s in cases if s.family == family]
        indices = special.get(family, list(range(2 if family in COMBINATIONS else 4)))
        chosen.extend(mine[index].scenario_id for index in indices)
    if len(chosen) != 80 or len(set(chosen)) != 80:
        raise ValueError('Smoke construction must contain exactly 80 cases')
    return chosen


def resolve(root, name):
    if type(name) is not str or not name or Path(name).is_absolute() or '..' in Path(name).parts:
        raise ValueError('Input path must stay inside the repository')
    path = root/name
    if not path.is_file() or path.is_symlink() or root.resolve() not in path.resolve().parents:
        raise ValueError('Input must be a regular repository file')
    return path


def verify(path, *, root=ROOT, pinned=None):
    from chaos.baselines.trace import unique_object
    if pinned is None:
        from chaos.qualification_pin import PINNED
        pinned = PINNED
    data = Path(path).read_bytes()
    if sha(data) != pinned:
        raise ValueError('Frozen manifest digest mismatch')
    manifest = json.loads(data, object_pairs_hook=unique_object)
    if manifest['format'] != 'safeact-qual-v1' or tuple(manifest['arms']) != ARMS:
        raise ValueError('Unsupported frozen comparison')
    for entry in manifest['inputs'].values():
        if sha(resolve(root, entry['path']).read_bytes()) != entry['sha256']:
            raise ValueError('Frozen input digest mismatch: '+entry['path'])
    inventory = json.loads(resolve(root, manifest['inputs']['source']['path']).read_text())['files']
    if sorted(entry['path'] for entry in inventory) != source_paths(root):
        raise ValueError('Frozen source inventory changed')
    for entry in inventory:
        if sha(resolve(root, entry['path']).read_bytes()) != entry['sha256']:
            raise ValueError('Frozen source changed: '+entry['path'])
    cases = [Scenario.from_json(line) for line in resolve(root, manifest['inputs']['scenarios']['path']).read_text().splitlines()]
    if (len(cases) != 1200 or digest(cases) != manifest['corpus_digest']
            or cases != scenarios() or manifest['smoke_ids'] != smoke_ids(cases)):
        raise ValueError('Frozen scenario definitions changed')
    return manifest, cases


def load_suspect(raw, row, *, cores):
    if raw.get('not_run') or row.get('not_applicable'):
        return []
    if not (row.get('harness_errors') or oracle.safety_failures(row)):
        return []
    reasons = []
    if set(row['family'].replace('x8', '').split('+')) & TIMING_SENSITIVE:
        reasons.append('timing_sensitive_family')
    samples = [raw.get('load_start', {}), raw.get('load_end', {})]
    # The common runner retains these under environment, with legacy support.
    samples += list(raw.get('load', {}).values())
    if any(isinstance(sample, dict) and sample.get('load_1', 0) > cores for sample in samples):
        reasons.append('load_above_core_count')
    scenario = raw.get('scenario', {})
    observed = raw.get('observation_finished', 0)-raw.get('started', 0)
    if observed > scenario.get('settle_seconds', float('inf'))+1:
        reasons.append('observation_overrun_above_one_second')
    return reasons


class RunControl:
    def __init__(self, seconds):
        self.deadline = time.monotonic()+seconds
        self.stopped = threading.Event()

    def allow(self):
        return not self.stopped.is_set() and time.monotonic() < self.deadline

    def record(self, row):
        if row.get('cleanup_incomplete') or (row.get('arm') == 'safeact' and oracle.safety_failures(row)):
            self.stopped.set()


def calibrated_slots(path, manifest, root=ROOT, *, select=False):
    from chaos.baselines.trace import unique_object
    calibration = json.loads(Path(path).read_text(), object_pairs_hook=unique_object)
    if calibration['manifest_sha256'] != sha((root/'chaos/manifest_qual_v1.json').read_bytes()):
        raise ValueError('Calibration refers to another manifest')
    records = calibration['candidates']
    expected = manifest['calibration']['candidates']
    if [r['slots'] for r in records] != expected:
        raise ValueError('Every frozen calibration candidate must be retained')
    eligible = []
    for record in records:
        base = resolve(root, record['environment_path'])
        if sha(base.read_bytes()) != record['environment_sha256']:
            raise ValueError('Calibration environment digest changed')
        environment_data = json.loads(base.read_text())
        if (environment_data.get('scope') != 'calibration' or environment_data.get('slots') != record['slots']
                or environment_data.get('manifest_sha256') != calibration['manifest_sha256']):
            raise ValueError('Calibration scope or slots differ')
        raw_path = resolve(root, record['raw_path'])
        if sha(raw_path.read_bytes()) != record['raw_sha256']:
            raise ValueError('Calibration raw digest changed')
        rows = [json.loads(line) for line in raw_path.read_text().splitlines()]
        pairs = {(r['scenario_id'], r['arm']) for r in rows}
        if len(rows) != 400 or pairs != {(s, arm) for s in manifest['smoke_ids'] for arm in ARMS}:
            raise ValueError('Incomplete calibration sample')
        if any(set(row.get('load', {})) != {'start', 'end'} for row in rows if not row.get('not_applicable')):
            raise ValueError('Missing calibration start/end load samples')
        loads = [sample['load_1'] for row in rows if not row.get('not_applicable') for sample in row['load'].values()]
        cores = environment_data['start']['cpu_count']
        if (type(cores) is not int or cores < 1 or not loads
                or any(type(load) not in (int, float) or not math.isfinite(load) or load < 0 for load in loads)):
            raise ValueError('Missing calibration load samples')
        if max(loads) < cores:
            eligible.append(record['slots'])
    if not eligible or (not select and calibration['selected_slots'] != max(eligible)):
        raise ValueError('Selected slot count does not follow the frozen load rule')
    return max(eligible)


def retain_unscheduled(cases, rows, out, classes):
    """Keep every requested denominator when a budget or safety stop occurs."""
    recorded = {(row['scenario_id'], row['arm']) for row in rows}
    from chaos.applicability import reason
    by_name = {arm.name: arm for arm in classes}
    with (out/'results.jsonl').open('a') as results, (out/'raw.jsonl').open('a') as raw:
        for case in cases:
            for arm in ARMS:
                if (case.scenario_id, arm) in recorded:
                    continue
                error = 'Not scheduled: primary comparison stopped or a slot terminated'
                row = dict(scenario_id=case.scenario_id, arm=arm, family=case.family,
                    connector=case.connector, expected_operations=len(case.operations),
                    completion_achievable=case.completion_achievable, operations=[],
                    not_run=True, harness_errors=[error])
                absent = reason(by_name[arm], case)
                if absent is not None:
                    row.update(not_applicable=True, not_applicable_reason=absent, harness_errors=[])
                rows.append(row)
                results.write(json.dumps(row)+'\n')
                raw.write(json.dumps(dict(row, scenario=json.loads(case.to_json())))+'\n')


def diagnostic_cases(directory, manifest_path, cases):
    base = Path(directory)
    metadata = json.loads((base/'environment.json').read_text())
    if metadata.get('scope') != 'full' or metadata.get('manifest_sha256') != sha(Path(manifest_path).read_bytes()):
        raise ValueError('Diagnostics require primary originals from the same frozen comparison')
    rows = [json.loads(line) for line in (base/'results.jsonl').read_text().splitlines()]
    indexed = {(row['scenario_id'], row['arm']): row for row in rows}
    if len(indexed) != len(rows):
        raise ValueError('Original results contain duplicate scenario/arm rows')
    by_id = {case.scenario_id: case for case in cases}
    chosen = set()
    for raw in [json.loads(line) for line in (base/'raw.jsonl').read_text().splitlines()]:
        identity = raw['scenario_id'], raw['arm']
        if identity not in indexed or raw['scenario_id'] not in by_id or raw['arm'] not in ARMS:
            raise ValueError('Original raw/results identity mismatch')
        if raw.get('scenario') != json.loads(by_id[raw['scenario_id']].to_json()):
            raise ValueError('Original scenario differs from the frozen definition')
        if load_suspect(raw, indexed[identity], cores=metadata['start']['cpu_count']):
            chosen.add(raw['scenario_id'])
    if not chosen:
        raise ValueError('No executed load-suspect failures to rerun')
    return [case for case in cases if case.scenario_id in chosen], dict(
        environment_sha256=sha((base/'environment.json').read_bytes()),
        raw_sha256=sha((base/'raw.jsonl').read_bytes()), results_sha256=sha((base/'results.jsonl').read_bytes()),
        primary_exit_code=metadata['exit_code'], headline_replacement=False,
        any_primary_safeact_safety_failure=any(row.get('arm') == 'safeact' and oracle.safety_failures(row) for row in rows))


def claim_invocation(manifest, manifest_sha256, scope, output, *, root=ROOT):
    """Durably claim the invocation budget before starting any owned service."""
    name = manifest['budget']['execution_ledger_dir']
    if type(name) is not str or Path(name).is_absolute() or '..' in Path(name).parts:
        raise ValueError('Execution ledger must remain inside the repository')
    directory = root/name
    directory.mkdir(parents=True, exist_ok=True)
    if root.resolve() not in directory.resolve().parents:
        raise ValueError('Execution ledger escaped the repository')
    filename = 'primary-full.json' if scope == 'full' else f'{scope}-{time.time_ns()}-{os.getpid()}.json'
    path = directory/filename
    try:
        fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise SystemExit('Primary run budget already claimed; preserve originals and use diagnostics or a disclosed new freeze')
    with os.fdopen(fd, 'w') as stream:
        json.dump(dict(manifest_sha256=manifest_sha256, scope=scope, output=str(Path(output).resolve()),
            started_at=time.time(), headline_input=scope == 'full'), stream)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    return path


def prepare_machine():
    """Refuse battery or unknown power; start the run's own caffeinate (macOS)."""
    power = environment.power_source()
    if power not in ('ac', 'no_battery'):
        raise SystemExit(f'Frozen runs refuse to start on battery or unknown power ({power})')
    try:
        return environment.hold_awake(os.getpid())
    except (OSError, RuntimeError) as error:
        raise SystemExit(f'Frozen runs require their own caffeinate: {error}')


def execute(args):
    # This is checked before manifest reading, service access or output creation.
    if os.environ.get('SAFEACT_RUN_FROZEN_QUALIFICATION') != '1':
        raise SystemExit('STOP 2 review required before any frozen smoke or full execution')
    if args.families or args.cases or args.limit or args.corpus != 'dev' or args.arms != 'safeact':
        raise SystemExit('Frozen comparisons do not accept development overrides')
    if args.scope is None:
        raise SystemExit('--scope smoke, calibration, diagnostic or full is required')
    manifest, cases = verify(args.manifest)
    if not 1 <= args.slots <= len(SLOT_REDIS_DBS):
        raise SystemExit('Unsupported slot count')
    if args.scope == 'full':
        if not args.calibration or args.slots != calibrated_slots(args.calibration, manifest):
            raise SystemExit('Full execution requires the verified slot-calibration amendment')
    elif args.scope == 'calibration' and args.slots not in manifest['calibration']['candidates']:
        raise SystemExit('Slot count is outside the frozen calibration plan')
    originals = None
    if args.scope == 'diagnostic':
        if not args.diagnostics_from or args.slots != 1:
            raise SystemExit('Diagnostic reruns require primary originals and one idle-machine slot')
        cases, originals = diagnostic_cases(args.diagnostics_from, args.manifest, cases)
    elif args.diagnostics_from:
        raise SystemExit('--diagnostics-from is only valid with diagnostic scope')
    if args.scope in ('smoke', 'calibration'):
        cases = [s for s in cases if s.scenario_id in manifest['smoke_ids']]
    from chaos.qualify import arms_named, exit_code, slot_worker
    classes = arms_named(list(ARMS))
    # Before any output or invocation claim: a refusal never spends the budget.
    awake, keeper = prepare_machine()
    start_environment = environment.describe()
    if args.scope == 'full':
        calibration = json.loads(Path(args.calibration).read_text())
        selected = next(record for record in calibration['candidates'] if record['slots'] == args.slots)
        calibrated_environment = json.loads(resolve(ROOT, selected['environment_path']).read_text())['start']
        fields = ('platform', 'machine', 'cpu_count', 'cpu_model', 'memory_bytes', 'memory')
        if any(start_environment.get(field) != calibrated_environment.get(field) for field in fields):
            raise SystemExit('Full execution requires calibration on this machine and OS')
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    claim = claim_invocation(manifest, sha(Path(args.manifest).read_bytes()), args.scope, out)
    description = dict(scope=args.scope, manifest_sha256=sha(Path(args.manifest).read_bytes()),
        start=start_environment, slots=args.slots, scenarios=len(cases), arms=ARMS,
        real_stripe_calls=0, paid_model_calls=0, power_source=start_environment['power_source'],
        caffeinate=awake)
    description['invocation_claim_path'] = str(claim.relative_to(ROOT))
    if originals:
        description['primary_originals'] = originals
    if args.calibration:
        description['calibration_sha256'] = sha(Path(args.calibration).read_bytes())
    work, errors, lock = queue.Queue(), [], threading.Lock()
    for case in cases:
        work.put(case)
    control = RunControl(manifest['budget']['wall_seconds'])
    with tempfile.TemporaryDirectory(prefix='safeact-frozen-') as temporary:
        with (out/'raw.jsonl').open('x') as raw, (out/'results.jsonl').open('x') as results:
            threads = [threading.Thread(target=slot_worker, args=(index, classes, work,
                dict(raw=raw, results=results), lock, Path(temporary), errors), kwargs=dict(control=control))
                for index in range(args.slots)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
    rows = [json.loads(line) for line in (out/'results.jsonl').read_text().splitlines()]
    retain_unscheduled(cases, rows, out, classes)
    status = exit_code(rows, len(cases)*len(ARMS))
    seen = {arm: set().union(*(set(row.get('interleavings', [])) for row in rows if row['arm'] == arm)) for arm in ARMS}
    missing = {arm.name: sorted(critical_labels(arm)-seen[arm.name]) for arm in classes}
    # Baseline guards may legitimately fail. SafeAct's full required coverage cannot.
    if args.scope == 'full' and missing['safeact']:
        status = 1
    (out/'coverage.json').write_text(json.dumps(dict(declared=coverage.CRITICAL,
        applicable={arm.name: sorted(critical_labels(arm)) for arm in classes},
        observed={name: sorted(labels) for name, labels in seen.items()}, missing=missing), indent=2)+'\n')
    summary = scoring.summarize(rows)
    summary.update(scope=args.scope, scoring_frozen=True, pending_metrics=[])
    if originals:
        summary.update(headline_input=False, primary_originals=originals)
    (out/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    (out/'report.md').write_text(report.render(summary))
    raw_rows = [json.loads(line) for line in (out/'raw.jsonl').read_text().splitlines()]
    boundaries = {arm: dict(commit_ack_stages=set(), concurrent_reclaim_refused=False) for arm in ARMS}
    for raw in raw_rows:
        detail = coverage.boundary_details(raw)
        boundaries[raw['arm']]['commit_ack_stages'].update(detail['commit_ack_stages'])
        boundaries[raw['arm']]['concurrent_reclaim_refused'] |= detail['concurrent_reclaim_refused']
    for detail in boundaries.values():
        detail['commit_ack_stages'] = sorted(detail['commit_ack_stages'])
    (out/'boundary-details.json').write_text(json.dumps(boundaries, indent=2)+'\n')
    if args.scope == 'full' and (not {'claim','fence','outcome'} <= set(boundaries['safeact']['commit_ack_stages'])
            or not boundaries['safeact']['concurrent_reclaim_refused']):
        status = 1
    indexed = {(row['scenario_id'], row['arm']): row for row in rows}
    diagnostics = [dict(scenario_id=raw['scenario_id'], arm=raw['arm'], reasons=reasons)
        for raw in raw_rows if (reasons := load_suspect(raw, indexed[(raw['scenario_id'], raw['arm'])],
            cores=description['start']['cpu_count']))]
    (out/'load-suspect.json').write_text(json.dumps(diagnostics, indent=2)+'\n')
    description.update(end=environment.describe(), caffeinate_end=environment.awake_status(keeper),
        exit_code=status, rows=len(rows),
        expected_rows=len(cases)*len(ARMS), not_run_rows=sum(bool(row.get('not_run')) for row in rows),
        stopped_for_budget_or_safety=not control.allow())
    (out/'environment.json').write_text(json.dumps(description, indent=2)+'\n')
    from deploy.evaluation.agent_v1.secret_scan import scan
    if scan([out]):
        status = description['exit_code'] = 1
        description['private_data_scan_failed'] = True
        (out/'environment.json').write_text(json.dumps(description, indent=2)+'\n')
    print(f'{args.scope}: {len(rows)}/{len(cases)*len(ARMS)} rows; exit={status}', flush=True)
    return status
