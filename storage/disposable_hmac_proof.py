"""Disposable-only HMAC proof prototype.

NOT for production authentication, credential isolation,
or permanent-schema deployment.
"""

import hmac
import re
import time
from hashlib import sha256
from secrets import token_hex

from psycopg import sql

from core.admission_authorization import RequestedIntent
from core.verified_identity import IdentityVerifier
from storage.submission import _prepare_payload
from storage.verified_disposable_admission import _validate_request


DOMAIN = "safeact-admission-proof-v1"

ARGUMENT_FIELDS = (
    "issuer", "subject", "request_key", "action_type",
    "resource_id", "connector_id", "provider_account_id",
    "contract_version", "payload_canonical", "payload_sha256",
    "provider_idempotency_key", "proof_key_id", "issued_at",
    "expires_at", "nonce", "mac",
)

SIGNED_FIELDS = ("domain", *ARGUMENT_FIELDS[:-1])


def _schema_identifier(schema):
    if (
        type(schema) is not str
        or re.fullmatch(r"safeact_it_[0-9a-f]{32}", schema) is None
    ):
        raise ValueError("Disposable schema required")
    return sql.Identifier(schema)


def encoded_part(value):
    if value is None:
        return "-1:"
    raw = str(value).encode("utf-8")
    return f"{len(raw)}:{raw.hex()}"


def proof_message(proof):
    if set(proof) != set(SIGNED_FIELDS) | {"mac"}:
        raise ValueError("Unexpected proof field inventory")
    return "".join(
        encoded_part(proof[name]) for name in SIGNED_FIELDS
    ).encode("utf-8")


def proof_parameters(proof):
    if type(proof["issued_at"]) is not int:
        raise ValueError("Invalid issuance timestamp")
    if type(proof["expires_at"]) is not int:
        raise ValueError("Invalid expiry timestamp")
    proof_message(proof)
    return tuple(proof[name] for name in ARGUMENT_FIELDS)


def issue_verified_proof(
    *,
    identity_verifier,
    token,
    request,
    signing_key,
    key_id="test-key-v1",
    issued_at=None,
    expires_at=None,
    nonce=None,
):
    """Experimental signer; takes a raw JWT, never an asserted principal."""
    if type(identity_verifier) is not IdentityVerifier:
        raise TypeError("IdentityVerifier required")
    if type(signing_key) is not bytes or len(signing_key) != 32:
        raise ValueError("32-byte test signing key required")
    if type(request) is not RequestedIntent:
        raise TypeError("RequestedIntent required")

    _validate_request(request)
    principal = identity_verifier.verify(token)
    canonical, digest = _prepare_payload(request.payload)

    issued = int(time.time()) if issued_at is None else issued_at
    expiry = issued + 60 if expires_at is None else expires_at
    proof_nonce = token_hex(16) if nonce is None else nonce

    if type(issued) is not int or type(expiry) is not int:
        raise ValueError("Integer timestamps required")

    proof = {
        "domain": DOMAIN,
        "issuer": principal.issuer,
        "subject": principal.subject,
        "request_key": request.request_key,
        "action_type": request.action_type,
        "resource_id": request.resource_id,
        "connector_id": request.connector_id,
        "provider_account_id": request.provider_account_id,
        "contract_version": request.contract_version,
        "payload_canonical": canonical,
        "payload_sha256": digest,
        "provider_idempotency_key": request.provider_idempotency_key,
        "proof_key_id": key_id,
        "issued_at": issued,
        "expires_at": expiry,
        "nonce": proof_nonce,
        "mac": None,
    }
    proof["mac"] = hmac.new(
        signing_key, proof_message(proof), sha256
    ).digest()
    proof_parameters(proof)
    return proof


def proof_part_sql(schema):
    s = _schema_identifier(schema)
    return sql.SQL("""
        CREATE FUNCTION {}.proof_part(p_value text)
        RETURNS text LANGUAGE sql IMMUTABLE
        SET search_path = pg_catalog
        AS $proof$
          SELECT CASE WHEN p_value IS NULL THEN '-1:'
          ELSE
            pg_catalog.octet_length(
              pg_catalog.convert_to(p_value,'UTF8')
            )::text || ':' ||
            pg_catalog.encode(
              pg_catalog.convert_to(p_value,'UTF8'),'hex'
            )
          END
        $proof$
    """).format(s)


def protected_gate_sql(schema):
    s = _schema_identifier(schema)
    return sql.SQL(r"""
        CREATE FUNCTION {s}.admit_with_proof(
            p_issuer text, p_subject text, p_key text,
            p_action text, p_resource text, p_connector text,
            p_account text, p_contract text, p_payload text,
            p_digest text, p_provider_key text,
            p_proof_key_id text, p_issued_at bigint,
            p_expires_at bigint, p_nonce text, p_mac bytea
        )
        RETURNS TABLE(operation_id uuid, created boolean)
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog
        AS $safeact_proof$
        DECLARE
            v_secret bytea;
            v_expected bytea;
            v_message text;
            v_now bigint;
            v_nonce text;
        BEGIN
            v_now := pg_catalog.floor(
                EXTRACT(EPOCH FROM pg_catalog.clock_timestamp())
            )::bigint;

            IF p_issued_at IS NULL OR p_expires_at IS NULL
               OR p_issued_at > v_now + 5
               OR p_issued_at < v_now - 120
               OR p_expires_at <= v_now
               OR p_expires_at <= p_issued_at
               OR p_expires_at - p_issued_at > 120
               OR p_nonce IS NULL
               OR p_nonce !~ '^[a-f0-9]{{32}}$'
               OR p_mac IS NULL
               OR pg_catalog.octet_length(p_mac) <> 32
            THEN
                RAISE EXCEPTION 'Proof denied'
                    USING ERRCODE='42501';
            END IF;

            SELECT k.signing_key INTO v_secret
            FROM {s}.proof_keys k
            WHERE k.key_id=p_proof_key_id AND k.enabled IS TRUE
            FOR SHARE;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'Proof denied'
                    USING ERRCODE='42501';
            END IF;

            SELECT pg_catalog.string_agg(
                {s}.proof_part(value),
                '' ORDER BY position
            ) INTO v_message
            FROM pg_catalog.unnest(
                ARRAY[
                    'safeact-admission-proof-v1',
                    p_issuer,p_subject,p_key,p_action,p_resource,
                    p_connector,p_account,p_contract,p_payload,
                    p_digest,p_provider_key,p_proof_key_id,
                    p_issued_at::text,p_expires_at::text,p_nonce
                ]
            ) WITH ORDINALITY AS fields(value,position);

            v_expected := public.hmac(
                pg_catalog.convert_to(v_message,'UTF8'),
                v_secret,'sha256'
            );

            IF v_expected IS DISTINCT FROM p_mac THEN
                RAISE EXCEPTION 'Proof denied'
                    USING ERRCODE='42501';
            END IF;

            INSERT INTO {s}.proof_nonces(nonce,expires_at)
            VALUES(p_nonce,p_expires_at)
            ON CONFLICT (nonce) DO NOTHING
            RETURNING nonce INTO v_nonce;

            IF v_nonce IS NULL THEN
                RAISE EXCEPTION 'Proof already consumed'
                    USING ERRCODE='42501';
            END IF;

            RETURN QUERY
            SELECT result.operation_id,result.created
            FROM {s}.admit_real_intent(
                p_issuer,p_subject,p_key,p_action,p_resource,
                p_connector,p_account,p_contract,p_payload,
                p_digest,p_provider_key
            ) result;
        END;
        $safeact_proof$
    """).format(s=s)


def proof_part_signature(schema):
    return sql.SQL("{}.proof_part(text)").format(
        _schema_identifier(schema)
    )


def proof_gate_signature(schema):
    return sql.SQL(
        "{}.admit_with_proof("
        "text,text,text,text,text,text,text,text,"
        "text,text,text,text,bigint,bigint,text,bytea)"
    ).format(_schema_identifier(schema))


def proof_call_sql(schema):
    return sql.SQL("""
        SELECT operation_id,created FROM {}.admit_with_proof(
            %s,%s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s,%s,%s,%s
        )
    """).format(_schema_identifier(schema))
