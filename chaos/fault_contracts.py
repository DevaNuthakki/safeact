"""Reject incompatible controls before any scenario fault or admission runs."""

from connectors.http_mock.admin_api import FAULTS


def validate_fault(fault):
    kind = fault.get('kind')
    providers = {'provider', 'later', 'hold', 'toxic', 'down', 'outage',
                 'rate_limit', 'reveal', 'hold_after_empty_lookup',
                 'hold_after_escalation', 'broker_redelivery'}
    if kind in providers and fault.get('provider') not in FAULTS:
        raise ValueError('Fault requires a supported mock provider')
    if kind in ('provider', 'later') and fault.get('switch') not in FAULTS[fault['provider']]:
        raise ValueError('Provider does not support this fault switch')
    if kind == 'reveal' and fault['provider'] not in ('payments', 'crm'):
        raise ValueError('Provider has no lookup index to reveal')
    if kind == 'workflow_lookup' and fault != {
            'kind': 'workflow_lookup', 'provider': 'payments',
            'step': 'compensation', 'switch': 'lookup_unavailable', 'value': True}:
        raise ValueError('Only an explicit inverse-payment lookup outage is supported')
    if kind not in providers | {'workflow', 'workflow_lookup', 'failpoint',
                                'worker_kill', 'broker_loss', 'broker_kill'}:
        raise ValueError('Unknown scenario fault kind')


def validate_scenario(scenario):
    for fault in scenario.faults:
        validate_fault(fault)
    inverse = [fault for fault in scenario.faults if fault['kind'] == 'workflow_lookup']
    modes = [fault['mode'] for fault in scenario.faults if fault['kind'] == 'workflow']
    if inverse and (scenario.workflow is None or len(inverse) != 1 or
                    modes not in (['lost_response'], ['hidden_response'])):
        raise ValueError('Inverse lookup fault requires an uncertain compensation workflow')
