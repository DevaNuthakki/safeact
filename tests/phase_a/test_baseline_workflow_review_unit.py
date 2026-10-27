"""Input/identity boundaries for B3 workflows and the local review surface."""

import unittest
from dataclasses import replace
from uuid import uuid4

from pydantic import ValidationError

from chaos.baselines.identity import TOKEN
from chaos.baselines.review_api import Decision, Login, Proposal
from chaos.baselines.workflow import WorkflowRequest, child


class BaselineWorkflowReviewUnitTests(unittest.TestCase):
    def request(self):
        return WorkflowRequest(tenant_id="north",requester_id="requester",request_key="business-intent",
            customer_id="customer",case_id="case",sku="replacement",fee_minor=500,
            payments_account_id="payments-account",crm_account_id="crm-account")

    def test_workflow_refuses_invalid_business_identity_and_noninteger_fee(self):
        request=self.request()
        for field,value in (("fee_minor",True),("fee_minor",0),("fee_minor",1.5),("fee_minor",2**31),
                            ("request_key",""),("request_key","two intents"),("sku"," "),
                            ("tenant_id",""),("requester_id","unknown human"),("case_id","")):
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):replace(request,**{field:value})

    def test_each_workflow_connector_must_support_its_exact_contract(self):
        for field in ("charge_contract","order_contract","compensation_contract"):
            with self.subTest(field=field),self.assertRaises(KeyError):replace(self.request(),**{field:"invented-contract"})

    def test_refund_requires_original_reference_and_has_a_separate_identity(self):
        request=self.request();workflow=uuid4()
        with self.assertRaises(ValueError):child(request,workflow,"compensation")
        steps=[child(request,workflow,s,charge_reference="actual-charge") for s in ("charge","order","compensation")]
        self.assertEqual(len({s.request_key for s in steps}),3)
        self.assertEqual(steps[-1].payload_canonical,'{"amount_minor":500,"charge_id":"actual-charge"}')
        self.assertEqual(steps[-1].provider_account_id,steps[0].provider_account_id)

    def test_review_models_refuse_identity_provider_proof_and_unlisted_actions(self):
        for value in (dict(action="ATTEST_APPLIED"),dict(action="REVERIFY",tenant_id="south"),
                      dict(action="PROTECTED_RETRY",reference="invented-proof"),dict(action="REVERIFY",ttl_seconds=0)):
            with self.subTest(value=value),self.assertRaises(ValidationError):Proposal.model_validate(value)

    def test_review_decision_boolean_and_binding_are_strict(self):
        original=dict(binding_sha256="a"*64,decision_key="one-click",approved=True)
        for change in (dict(approved="true"),dict(approved=1),dict(binding_sha256="é"*64),dict(decision_key="two clicks"),
                       dict(reviewer_id="someone-else"),dict(evidence="invented")):
            with self.subTest(change=change),self.assertRaises(ValidationError):Decision.model_validate(dict(original,**change))

    def test_password_model_repr_and_session_namespace_do_not_accept_service_keys(self):
        secret="synthetic-human-password"
        self.assertNotIn(secret,repr(Login(username="human",password=secret)))
        for value in ("invented-agent-key","sak_"+"a"*64,"Bearer token","sb3s_short"):
            self.assertIsNone(TOKEN.fullmatch(value))


if __name__=="__main__":unittest.main()
