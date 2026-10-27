"""Mutate saved local B3 observations; no providers or database are contacted."""

import argparse
import copy
import json
from pathlib import Path

from chaos import oracle
from chaos.scenarios import Scenario


def check(root):
    value=json.loads((Path(root)/'raw/test_actual_inverse_with_distinct_provider_accounts_passes_the_common_oracle.json').read_text())
    original=value['raw'];case=Scenario.from_json(json.dumps(original['scenario']));results=[]
    mutations=[('request:'+field,field) for field in (
        'tenant_id','requester_id','request_key','customer_id','case_id','sku','fee_minor',
        'payments_account_id','crm_account_id','charge_contract','order_contract','compensation_contract')]
    mutations += [('inverse:'+field,field) for field in (
        'tenant_id','requester_id','request_key','resource_id','provider_account_id','contract_version','compensates_operation_id')]
    mutations += [(name,None) for name in ('missing_precursor','uncertain_precursor','missing_inverse_effect',
        'duplicate_inverse_effect','unknown_representation','missing_committed_binding','wrong_refund_amount','wrong_charge_reference')]
    for name,field in mutations:
        raw=copy.deepcopy(original)
        samples=[e for e in raw['database'] if e['event']=='db_workflow_sample']
        inverse=next(s for s in samples[-1]['steps'] if s['step']=='compensation')
        if name.startswith('request:'):samples[-1]['request'][field]='unrelated'
        elif name.startswith('inverse:'):inverse[field]='unrelated'
        elif name=='missing_precursor':raw['database']=[samples[-1]]
        elif name=='uncertain_precursor':
            for sample in samples[:-1]:next(s for s in sample['steps'] if s['step']=='order')['state']='UNKNOWN'
        elif name in ('missing_inverse_effect','duplicate_inverse_effect'):
            effect=next(e for e in raw['ledger'] if e['event']=='effect' and e['correlation_sha256']==inverse['correlation_sha256'])
            if name=='missing_inverse_effect':raw['ledger'].remove(effect)
            else:raw['ledger'].append(dict(effect,effect_id='duplicate-inverse-effect'))
        elif name=='unknown_representation':samples[-1]['binding_format']='unknown'
        elif name=='missing_committed_binding':raw['database']=[]
        elif name=='wrong_refund_amount':inverse['payload']['amount_minor']+=1
        elif name=='wrong_charge_reference':inverse['payload']['charge_id']='unrelated-charge'
        row=oracle.evaluate(case,raw)
        assert not row['operations'][0]['safe_completion'],name
        assert oracle.safety_failures(row) or row['harness_errors'],name
        results.append(dict(mutation=name,rejected=True,safety_failures=oracle.safety_failures(row),harness_errors=row['harness_errors']))
    return dict(scope='Synthetic mutations of saved observations; not execution, coverage or additional unittest cases',
                cases=len(results),rejected=len(results),results=results)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root');p.add_argument('--out');a=p.parse_args()
    value=check(a.root)
    if a.out:Path(a.out).write_text(json.dumps(value,indent=2)+'\n')
    print(value['cases'],'counterexamples rejected')
