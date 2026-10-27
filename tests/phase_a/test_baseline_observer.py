"""Common workflow scoring rejects forged native B3 bindings."""

import unittest

from chaos import oracle
from tests.phase_a.test_workflow_qualification import fixture


def native_fixture():
    case,raw=fixture();raw['arm']='b3';spec=case.workflow
    for sample in raw['database']:
        sample.pop('plan');sample.update(binding_format='b3-request-v1',tenant_id='tenant',
            requester_id='requester',request_key='business-key',request=dict(tenant_id='tenant',
                requester_id='requester',request_key='business-key',customer_id=spec.customer_id,
                case_id=spec.case_id,sku=spec.sku,fee_minor=case.operations[0].amount_minor,
                payments_account_id='payments-account',crm_account_id='crm-account',
                charge_contract=spec.charge_contract,order_contract=spec.order_contract,
                compensation_contract=spec.compensation_contract))
        for step in sample['steps']:
            role=step['step'];step.update(tenant_id='tenant',requester_id='requester',
                request_key='wf:workflow:'+role,
                provider_account_id='crm-account' if role=='order' else 'payments-account')
            if role=='compensation':step['resource_id']='charge-effect'
    return case,raw


class NativeWorkflowOracleTests(unittest.TestCase):
    def test_native_representation_passes_without_changing_actual_inverse_resource(self):
        case,raw=native_fixture();row=oracle.evaluate(case,raw)
        self.assertFalse(oracle.safety_failures(row),row)
        self.assertTrue(row['operations'][0]['safe_completion'])

    def test_unknown_or_malformed_representations_fail_validation(self):
        for replacement in ('unknown',None,12):
            case,raw=native_fixture()
            raw['database'][-1]['binding_format']=replacement
            with self.subTest(representation=replacement):
                self.assertTrue(oracle.safety_failures(oracle.evaluate(case,raw)))
        for replacement in (None,[],{},'request'):
            case,raw=native_fixture();raw['database'][-1]['request']=replacement
            with self.subTest(request=replacement):
                self.assertTrue(oracle.safety_failures(oracle.evaluate(case,raw)))

    def test_root_request_fields_and_original_accounts_are_bound(self):
        case,raw=native_fixture();fields=list(raw['database'][-1]['request'])
        for field in fields:
            case,raw=native_fixture();raw['database'][-1]['request'][field]='unrelated'
            with self.subTest(field=field):
                self.assertTrue(oracle.safety_failures(oracle.evaluate(case,raw)))

    def test_every_child_is_bound_to_its_tenant_requester_and_step_identity(self):
        for role in ('charge','order','compensation'):
            for field in ('tenant_id','requester_id','request_key','provider_account_id','resource_id','contract_version'):
                case,raw=native_fixture()
                next(s for s in raw['database'][-1]['steps'] if s['step']==role)[field]='unrelated'
                with self.subTest(role=role,field=field):
                    self.assertTrue(oracle.safety_failures(oracle.evaluate(case,raw)))

    def test_inverse_payload_amount_reference_and_link_remain_required(self):
        for field in ('amount_minor','charge_id','compensates_operation_id'):
            case,raw=native_fixture();step=raw['database'][-1]['steps'][-1]
            if field=='compensates_operation_id':step[field]='unrelated'
            else:step['payload'][field]='unrelated'
            with self.subTest(field=field):
                row=oracle.evaluate(case,raw)
                self.assertTrue(oracle.safety_failures(row));self.assertEqual(row['invalid_compensation_effects'],1)

    def test_missing_pre_effect_sample_or_uncertain_order_cannot_prove_inverse(self):
        for mutation in ('missing','uncertain','late'):
            case,raw=native_fixture()
            if mutation=='missing':raw['database'].pop(0)
            elif mutation=='uncertain':raw['database'][0]['steps'][1]['state']='UNKNOWN'
            else:raw['database'][0].update(sample_started_ns=35,sample_finished_ns=36,monotonic_ns=36)
            with self.subTest(mutation=mutation):
                row=oracle.evaluate(case,raw)
                self.assertTrue(oracle.safety_failures(row));self.assertEqual(row['invalid_compensation_effects'],1)

    def test_forged_plan_cannot_replace_a_mismatched_native_request(self):
        case,raw=native_fixture();_,safeact=fixture()
        raw['database'][-1]['plan']=safeact['database'][-1]['plan']
        raw['database'][-1]['request']['sku']='changed'
        self.assertTrue(oracle.safety_failures(oracle.evaluate(case,raw)))

    def test_malformed_legacy_plan_is_a_failure_not_a_scoring_crash(self):
        for replacement in (None,[],dict(order=dict(payload_canonical='{'))):
            case,raw=fixture();raw['database'][-1]['plan']=replacement
            with self.subTest(plan=replacement):
                self.assertTrue(oracle.safety_failures(oracle.evaluate(case,raw)))
