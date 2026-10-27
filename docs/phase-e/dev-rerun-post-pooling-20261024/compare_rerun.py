"""Compare post-pooling development rows with the selected pre-pooling rows.

Usage: python compare_rerun.py PRE_RESULTS POST_RESULTS OUT_JSON
Compares every (scenario, arm) on outcome fields only; seconds and other
timing values are reported separately, never as a difference.
"""
import json
import sys
from collections import Counter, defaultdict

from chaos.qualify import DEV_COUNTS, DEV_SEED
from chaos.scenarios import corpus
from chaos.scoring import summarize

SAFETY = ("prohibited_duplicates", "orphan_effects", "unaccounted", "changed_accepted",
          "false_claims", "compensation_duplicates", "invalid_compensation_effects")


def outcome(row):
    if row.get("not_applicable"):
        return dict(not_applicable=True)
    ops = row.get("operations") or []
    return dict(
        not_applicable=False,
        safety={k: row.get(k, 0) for k in SAFETY},
        integrity=bool(row.get("integrity")),
        harness_errors=bool(row.get("harness_errors")),
        cleanup_incomplete=bool(row.get("cleanup_incomplete")),
        outcome_classes=[o.get("outcome_class") for o in ops],
        effects=[o.get("effects") for o in ops],
        safe_completion=[o.get("safe_completion") for o in ops],
        interleavings=sorted(set(row.get("interleavings") or [])),
    )


def load(path):
    return {(r["scenario_id"], r["arm"]): r for r in map(json.loads, open(path))}


pre, post = load(sys.argv[1]), load(sys.argv[2])
seeds = {s.scenario_id: s.seed for s in corpus("dev", DEV_COUNTS, DEV_SEED)}
families = {s.scenario_id: s.family for s in corpus("dev", DEV_COUNTS, DEV_SEED)}
differences = []
for key in sorted(set(pre) | set(post)):
    a, b = pre.get(key), post.get(key)
    if a is None or b is None:
        differences.append(dict(scenario_id=key[0], arm=key[1], seed=seeds.get(key[0]),
                                fields=["row_missing_" + ("pre" if a is None else "post")]))
        continue
    oa, ob = outcome(a), outcome(b)
    changed = sorted(k for k in set(oa) | set(ob) if oa.get(k) != ob.get(k))
    if changed:
        differences.append(dict(scenario_id=key[0], arm=key[1], family=families.get(key[0]),
                                seed=seeds.get(key[0]), fields=changed,
                                pre={k: oa.get(k) for k in changed},
                                post={k: ob.get(k) for k in changed},
                                pre_source=a.get("development_source")))


def by_family(rows):
    out = {}
    grouped = defaultdict(list)
    for r in rows.values():
        grouped[r["family"]].append(r)
    for family, items in sorted(grouped.items()):
        s = summarize(items)["by_arm"]
        out[family] = {arm: dict(rows=sum(1 for r in items if r["arm"] == arm),
                                 not_applicable=sum(1 for r in items if r["arm"] == arm and r.get("not_applicable")),
                                 safety_failure_rows=c["safety_failure_rows"],
                                 useful=f'{c["usefulness"]["completed"]}/{c["usefulness"]["achievable"]}')
                       for arm, c in s.items()}
    return out


result = dict(
    scope="Existing development cases only (corpus dev, seed 7); no frozen corpus, smoke, calibration or full run",
    compared_pairs=len(set(pre) & set(post)), pre_rows=len(pre), post_rows=len(post),
    differences=differences, differing_pairs=len(differences),
    differing_by_arm=dict(Counter(d["arm"] for d in differences)),
    pre_by_family=by_family(pre), post_by_family=by_family(post),
)
json.dump(result, open(sys.argv[3], "w"), indent=2)
print(json.dumps(dict(compared=result["compared_pairs"], differing=len(differences),
                      by_arm=result["differing_by_arm"])))
