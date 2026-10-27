"""Declared wire identities for each baseline; callers never choose them."""

from uuid import UUID

from chaos.profiles import profile


def payment_key(operation_id,ordinal=1,mode='b3'):
    if mode not in ('b0','b1','b2','b3') or type(ordinal) is not int or ordinal<1:raise ValueError('Invalid baseline key strategy')
    root=UUID(str(operation_id)).hex
    return f'{mode}_{root}_{ordinal}' if mode in ('b0','b1') else f'{mode}_{root}'


def registration_keys(operation_id,connector,version,mode='b3'):
    contract=profile(connector,version)
    if not contract.contract.native_idempotency:return ()
    return tuple(payment_key(operation_id,n,mode) for n in range(1,contract.max_attempts+1)) if mode in ('b0','b1') else (payment_key(operation_id,1,mode),)
