"""Bounded Phase A operation and connector contracts.

Provider adapters, not agents or user-controlled inputs,
must normalize evidence.

These in-memory types do not replace durable authorization
or database constraints.
"""

from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Any


def canonical_payload_bytes(payload: dict[str, Any]) -> bytes:
    """Serialize a restricted JSON object without floating-point values.

    This is a project-local canonical form, not RFC 8785.
    Incoming raw JSON must first be checked for duplicate keys
    at the HTTP boundary.
    """

    def check(value: Any) -> None:
        if type(value) is dict:
            if not all(type(key) is str for key in value):
                raise ValueError("payload keys must be strings")
            for child in value.values():
                check(child)
        elif type(value) is list:
            for child in value:
                check(child)
        elif type(value) not in (str, int, bool, type(None)):
            raise ValueError(
                "payload must contain JSON primitives without floats"
            )

    if type(payload) is not dict:
        raise ValueError("payload must be a JSON object")

    check(payload)

    wire = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")

    return wire


def canonical_payload_hash(payload: dict[str, Any]) -> str:
    """Hash the same canonical UTF-8 bytes used for storage."""
    return sha256(canonical_payload_bytes(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class OperationIdentity:
    operation_id: str
    tenant_id: str
    action_type: str
    resource_id: str
    provider_account_id: str
    connector_id: str
    payload_sha256: str

    def __post_init__(self) -> None:
        fields = (
            "operation_id",
            "tenant_id",
            "action_type",
            "resource_id",
            "provider_account_id",
            "connector_id",
        )

        for name in fields:
            value = getattr(self, name)

            if (
                type(value) is not str
                or not value
                or value != value.strip()
            ):
                raise ValueError(
                    f"{name} must be a nonblank normalized string"
                )

        if (
            type(self.payload_sha256) is not str
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                self.payload_sha256,
            )
        ):
            raise ValueError(
                "payload_sha256 must be a lowercase SHA-256 digest"
            )


class IdempotencyKeyScope(str, Enum):
    PROVIDER_ACCOUNT_ENDPOINT = "provider-account-and-endpoint"
    PROVIDER_ACCOUNT = "provider-account"
    GLOBAL = "global"


@dataclass(frozen=True, slots=True)
class NativeIdempotency:
    key_scope: IdempotencyKeyScope
    retention: timedelta
    safety_margin: timedelta
    protects_concurrent_same_key: bool
    binds_parameters: bool
    contract_reference: str

    def __post_init__(self) -> None:
        if not isinstance(
            self.key_scope, IdempotencyKeyScope
        ):
            raise ValueError(
                "idempotency key scope must be a known enum"
            )

        if type(self.binds_parameters) is not bool:
            raise ValueError(
                "parameter-binding guarantee must be explicit"
            )

        if (
            type(self.contract_reference) is not str
            or not self.contract_reference.strip()
        ):
            raise ValueError(
                "contract reference must be documented"
            )

        if (
            not isinstance(self.retention, timedelta)
            or self.retention <= timedelta(0)
        ):
            raise ValueError("retention must be positive")

        if (
            not isinstance(self.safety_margin, timedelta)
            or not timedelta(0)
            <= self.safety_margin
            < self.retention
        ):
            raise ValueError(
                "safety margin must be nonnegative "
                "and below retention"
            )

        if type(self.protects_concurrent_same_key) is not bool:
            raise ValueError(
                "concurrency protection must be explicit"
            )


class LookupSemantics(str, Enum):
    UNAVAILABLE = "unavailable"
    EVENTUAL = "eventual"
    STRONG = "strong"


@dataclass(frozen=True, slots=True)
class ConnectorContract:
    connector_id: str
    version: str
    native_idempotency: NativeIdempotency | None
    lookup_semantics: LookupSemantics

    def __post_init__(self) -> None:
        if (
            type(self.connector_id) is not str
            or not self.connector_id.strip()
        ):
            raise ValueError("connector id must be provided")

        if (
            type(self.version) is not str
            or not self.version.strip()
        ):
            raise ValueError("contract version must be provided")

        if (
            self.native_idempotency is not None
            and not isinstance(
                self.native_idempotency,
                NativeIdempotency,
            )
        ):
            raise ValueError(
                "native_idempotency must be a documented contract"
            )

        if not isinstance(
            self.lookup_semantics,
            LookupSemantics,
        ):
            raise ValueError(
                "lookup semantics must be documented"
            )


class EvidenceKind(str, Enum):
    APPLIED = "applied"
    PROVEN_NOT_APPLIED = "proven_not_applied"
    NOT_FOUND = "not_found"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: EvidenceKind
    connector_id: str
    operation_id: str
    provider_account_id: str
    contract_version: str
    reference: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EvidenceKind):
            raise ValueError("unknown evidence kind")

        fields = (
            "connector_id",
            "operation_id",
            "provider_account_id",
            "contract_version",
        )

        for name in fields:
            value = getattr(self, name)

            if (
                type(value) is not str
                or not value.strip()
            ):
                raise ValueError(
                    f"{name} must be provided"
                )

        decisive = (
            EvidenceKind.APPLIED,
            EvidenceKind.PROVEN_NOT_APPLIED,
        )

        if self.kind in decisive:
            if (
                type(self.reference) is not str
                or not self.reference.strip()
            ):
                raise ValueError(
                    "decisive evidence requires a reference"
                )
        elif self.reference is not None:
            raise ValueError(
                "inconclusive evidence cannot carry "
                "a decisive reference"
            )
