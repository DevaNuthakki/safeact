"""Server-side provider idempotency keys, derived from operation identity.

A caller never chooses a provider key. The key is a pure function of the
operation's immutable identity (tenant, request key, connector, provider
account), so a resubmission of the same operation gets the same key and
two distinct operations can never share one. Migration 0008 recomputes
the same derivation in a CHECK constraint, so no admission path can
store any other value.

Only connectors whose contract declares native idempotency get a key.
"""

from hashlib import sha256

from core.connector_profiles import PAYMENTS, STRIPE


# Connectors whose provider honours an idempotency key.
NATIVE_KEY_CONNECTORS = frozenset({PAYMENTS, STRIPE})

KEY_PREFIX = "sa1_"
_SEPARATOR = "\x1f"  # unit separator: cannot appear in identity fields


def derive_provider_key(
    tenant_id, request_key, connector_id, provider_account_id
):
    if connector_id not in NATIVE_KEY_CONNECTORS:
        return None
    material = _SEPARATOR.join(
        (tenant_id, request_key, connector_id, provider_account_id)
    )
    return KEY_PREFIX + sha256(material.encode("utf-8")).hexdigest()
