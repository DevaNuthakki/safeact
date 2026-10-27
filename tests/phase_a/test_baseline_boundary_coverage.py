"""Native B3 facts must substantiate shared human-review meanings."""

import copy
import unittest

from chaos.baselines.review_basis import digest
from chaos.boundary_coverage import observed
from chaos import oracle
from tests.phase_a.test_boundary_qualification import review_fixture


def native_fixture(mode='concurrent'):
    case,raw=review_fixture(mode);raw=copy.deepcopy(raw);raw['arm']='b3'
    proposal=raw['database'][0]['proposals'][0]
    policy=dict(requires_retry_review=mode=='protected_retry',revision=1 if mode=='protected_retry' else 0,configured_override=False)
    if mode=='protected_retry':proposal['bound_state']='UNKNOWN'
    native=dict(proposal_id=proposal['proposal_id'],operation_id='op',proposer_id=proposal['proposed_by'],
        action=proposal['action'],basis=dict(version=proposal['bound_seq'],state=proposal['bound_state'],
            execution=dict(policy_sha256=digest(policy),attempt_count=1)),
        expires_at='2099-01-01 00:00:00+00:00')
    for sample in raw['database']:
        sample['binding_format']='b3-review-v1'
        sample['review_policy']=policy
        if sample['state']=='VERIFYING':sample['state']='UNKNOWN'
        for p in sample['proposals']:p.update(native=native,binding_digest=digest(native))
        if sample['label']=='race_decided':sample['seq']=6
        for audit in sample['audit']:
            old=audit.pop('event_type');audit['audit_format']='b3-review-v1'
            audit['native_kind']={'DECISION_RECORDED':'decision_recorded','APPROVAL_REFUSED_STALE':'decision_refused_stale'}[old]
            audit['native_version']=6;audit['details']={'proposal_id':'proposal','approved':old=='DECISION_RECORDED'}
        if sample['label']=='review_complete':
            retry=mode=='protected_retry'
            sample['native_authorizations']=[dict(proposal_id='proposal',action=proposal['action'],next_ordinal=2,
                basis_sha256=digest(native['basis']['execution']))]
            sample['native_authorization_uses']=[dict(proposal_id='proposal',attempt_id='retry' if retry else None,
                lookup_token=None if retry else 'lookup-token')]
            sample['native_attempts']=[dict(attempt_id='retry',ordinal=2)] if retry else []
            if not retry:sample['evidence'].append(dict(native_source='lookup',native_kind='applied',lookup_token='lookup-token'))
    for call in raw['boundary']:
        if call['event']=='reviewers_blocked':continue
        call['boundary_format']='b3-review-v1';call['binding_digest']=digest(native)
        if call['event']=='proposal_displayed':call['native_body']=dict(proposal_id='proposal',binding_sha256=digest(native))
        else:
            status='accepted' if call.get('outcome')=='approved' else call.get('outcome') or ('stale' if mode=='stale' else 'already_decided')
            call['native_body']=dict(proposal_id='proposal',status=status)
    return case,raw


class BaselineBoundaryCoverageTests(unittest.TestCase):
    def test_native_review_modes_keep_shared_meaning_without_fake_audit_kinds(self):
        for mode,label in (('concurrent','reviewers_serialized_on_binding'),('stale','stale_review_refused_after_evidence'),
                ('protected_retry','reviewed_retry_after_proven_nonapplication')):
            case,raw=native_fixture(mode)
            with self.subTest(mode=mode):self.assertIn(label,observed(raw))
            self.assertTrue(all('event_type' not in a for s in raw['database'] for a in s['audit']))

    def test_changed_native_binding_or_missing_committed_audit_never_qualifies(self):
        for change in (lambda r:r['database'][0]['proposals'][0]['native']['basis'].update(version=99),
                lambda r:r['database'][0]['proposals'][0].update(binding_digest='0'*64),
                lambda r:[s.update(audit=[]) for s in r['database']],
                lambda r:[a.update(native_version=5) for s in r['database'] for a in s['audit']]):
            _,raw=native_fixture();change(raw)
            self.assertNotIn('reviewers_serialized_on_binding',observed(raw))

    def test_normalized_winner_cannot_override_a_native_refusal(self):
        _,raw=native_fixture()
        next(c for c in raw['boundary'] if c.get('outcome')=='approved')['native_body']['status']='already_decided'
        self.assertNotIn('reviewers_serialized_on_binding',observed(raw))

    def test_forged_native_display_or_replay_is_not_credit(self):
        for event in ('proposal_displayed','decision_replayed'):
            _,raw=native_fixture();next(c for c in raw['boundary'] if c['event']==event)['native_body']['proposal_id']='foreign'
            self.assertNotIn('reviewers_serialized_on_binding',observed(raw))

    def test_stale_refusal_needs_a_later_native_committed_version(self):
        _,raw=native_fixture('stale')
        for sample in raw['database']:
            for audit in sample['audit']:audit['native_version']=5
        self.assertNotIn('stale_review_refused_after_evidence',observed(raw))

    def test_permission_requires_its_single_native_consumption_record(self):
        for field in ('native_authorizations','native_authorization_uses','native_attempts'):
            _,raw=native_fixture('protected_retry')
            next(s for s in raw['database'] if s['label']=='review_complete')[field]=[]
            self.assertNotIn('reviewed_retry_after_proven_nonapplication',observed(raw))

    def test_missing_native_boundary_blocks_common_scoring_with_a_correct_effect(self):
        case,raw=native_fixture()
        raw.update(started=100,finished=110,timeline=[],integrity=[],
            operations={'op':dict(state='SUCCEEDED',outcome_class='confirmed_success',evidence_rows=1,resolved_at=109)})
        for event in raw['ledger']:event.update(at=101,resource=case.operations[0].resource_id)
        self.assertEqual(oracle.evaluate(case,raw)['harness_errors'],[])
        raw['boundary']=[c for c in raw['boundary'] if c['event']!='reviewers_blocked']
        value=oracle.evaluate(case,raw)
        self.assertTrue(any('Missing boundary observations' in e for e in value['harness_errors']))
        self.assertFalse(value['operations'][0]['safe_completion'])
