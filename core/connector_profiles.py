"""Versioned connector profiles: the contract every decision is bound to.

A profile extends the Phase A ``ConnectorContract`` (native idempotency,
lookup semantics) with dispatch limits, investigation timing and the
retry and reopen budgets. Profiles are persisted, immutable, in
``safeact_connector_contracts``; the protected functions read them
from there, never from the caller.

The three mock profiles describe in-process mocks, not real providers.
The Stripe profile describes Stripe's documented API v1 behaviour in a
sandbox (test mode); it is registered only by the sandbox runner in a
disposable schema. The public schema registers no profile, so nothing
can dispatch there.
"""

from dataclasses import dataclass
from datetime import timedelta
from enum import Enum

from core.contracts import (
    ConnectorContract,
    IdempotencyKeyScope,
    LookupSemantics,
    NativeIdempotency,
)


PAYMENTS = "mock-payments"
CRM = "mock-crm"
EMAIL = "mock-email"
STRIPE = "stripe-test"

# Pinned per request with the Stripe-Version header, never the account
# default (docs.stripe.com/upgrades). Read and chosen 2026-10-20.
STRIPE_API_VERSION = "2026-09-30.endive"


class SideEffectClass(str, Enum):
    REVERSIBLE_PAYMENT = "reversible_payment"
    CRM_RECORD = "crm_record"
    IRREVERSIBLE_MESSAGE = "irreversible_message"


def _whole_seconds(value, name, *, positive):
    if (
        not isinstance(value, timedelta)
        or value.microseconds
        or value < timedelta(0)
        or (positive and value == timedelta(0))
    ):
        raise ValueError(f"{name} must be whole, nonnegative seconds")
    return int(value.total_seconds())


def _seconds(value, name):
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise ValueError(f"{name} must be a positive duration")
    if value.microseconds % 1000:
        raise ValueError(f"{name} must be whole milliseconds")
    return value


@dataclass(frozen=True, slots=True)
class ConnectorLimits:
    """F16 limits (0018), per contract; None on a profile means none.

    dispatch_rate/burst: a token bucket on claims per provider account.
    backoff base/cap: retry delay base * 2^(attempts-1), capped, with
    deterministic full jitter. lookup interval/cap: reconciler lookups
    after a dispatch, doubling per lookup, capped, never past the
    investigation deadline. breaker threshold/cooldown: consecutive
    failing dispatch outcomes that open the breaker, and for how long.
    """

    backoff_base: timedelta
    backoff_cap: timedelta
    lookup_interval: timedelta
    lookup_interval_cap: timedelta
    dispatch_rate_per_second: float | None = None
    dispatch_burst: int | None = None
    breaker_threshold: int | None = None
    breaker_cooldown: timedelta | None = None

    def __post_init__(self):
        _seconds(self.backoff_base, "backoff base")
        _seconds(self.backoff_cap, "backoff cap")
        _seconds(self.lookup_interval, "lookup interval")
        _seconds(self.lookup_interval_cap, "lookup interval cap")
        if self.backoff_cap < self.backoff_base:
            raise ValueError("backoff cap must be at least the base")
        if self.lookup_interval_cap < self.lookup_interval:
            raise ValueError("lookup cap must be at least the interval")
        if (self.dispatch_rate_per_second is None) != (
                self.dispatch_burst is None):
            raise ValueError("rate and burst go together")
        if self.dispatch_rate_per_second is not None and (
            not 0 < float(self.dispatch_rate_per_second) <= 10000
            or type(self.dispatch_burst) is not int or self.dispatch_burst < 1
        ):
            raise ValueError("rate must be positive and burst at least 1")
        if (self.breaker_threshold is None) != (self.breaker_cooldown is None):
            raise ValueError("breaker threshold and cooldown go together")
        if self.breaker_threshold is not None:
            if type(self.breaker_threshold) is not int or (
                    self.breaker_threshold < 1):
                raise ValueError("breaker threshold must be at least 1")
            _seconds(self.breaker_cooldown, "breaker cooldown")

    def row(self, connector_id, version):
        seconds = lambda v: (  # noqa: E731
            None if v is None else round(v.total_seconds(), 3))
        return {
            "connector_id": connector_id,
            "version": version,
            "dispatch_rate_per_second": self.dispatch_rate_per_second,
            "dispatch_burst": self.dispatch_burst,
            "backoff_base_seconds": seconds(self.backoff_base),
            "backoff_cap_seconds": seconds(self.backoff_cap),
            "lookup_interval_seconds": seconds(self.lookup_interval),
            "lookup_interval_cap_seconds": seconds(self.lookup_interval_cap),
            "breaker_threshold": self.breaker_threshold,
            "breaker_cooldown_seconds": seconds(self.breaker_cooldown),
        }


# Column order of safeact_contract_limits (0018).
LIMITS_COLUMNS = (
    "connector_id", "version", "dispatch_rate_per_second", "dispatch_burst",
    "backoff_base_seconds", "backoff_cap_seconds",
    "lookup_interval_seconds", "lookup_interval_cap_seconds",
    "breaker_threshold", "breaker_cooldown_seconds",
)


def scaled_limits(limits, factor):
    """The same limits with every duration multiplied by ``factor``."""

    def scale(value):
        if value is None:
            return None
        millis = value / timedelta(milliseconds=1)
        return timedelta(milliseconds=max(1, round(millis * factor)))

    rate = limits.dispatch_rate_per_second
    return ConnectorLimits(
        backoff_base=scale(limits.backoff_base),
        backoff_cap=scale(limits.backoff_cap),
        lookup_interval=scale(limits.lookup_interval),
        lookup_interval_cap=scale(limits.lookup_interval_cap),
        dispatch_rate_per_second=None if rate is None
        else round(rate / factor, 3),
        dispatch_burst=limits.dispatch_burst,
        breaker_threshold=limits.breaker_threshold,
        breaker_cooldown=scale(limits.breaker_cooldown),
    )


@dataclass(frozen=True, slots=True)
class ConnectorProfile:
    contract: ConnectorContract
    side_effect_class: SideEffectClass
    request_deadline: timedelta
    investigation_window: timedelta
    post_escalation_check_interval: timedelta
    post_escalation_horizon: timedelta
    max_attempts: int
    max_reopens: int
    compensation_action: str | None
    contract_reference: str
    # F16 limits (0018); None: the contract declares none.
    limits: ConnectorLimits | None = None

    def __post_init__(self):
        if self.limits is not None and not isinstance(
                self.limits, ConnectorLimits):
            raise ValueError("limits must be ConnectorLimits")
        if not isinstance(self.contract, ConnectorContract):
            raise ValueError("a typed connector contract is required")
        if not isinstance(self.side_effect_class, SideEffectClass):
            raise ValueError("side-effect class must be a known enum")

        _whole_seconds(
            self.request_deadline, "request deadline", positive=True
        )
        _whole_seconds(
            self.investigation_window, "investigation window",
            positive=False,
        )
        _whole_seconds(
            self.post_escalation_check_interval,
            "post-escalation check interval", positive=True,
        )
        _whole_seconds(
            self.post_escalation_horizon, "post-escalation horizon",
            positive=False,
        )

        native = self.contract.native_idempotency
        if native is not None:
            _whole_seconds(native.retention, "retention", positive=True)
            _whole_seconds(
                native.safety_margin, "safety margin", positive=False
            )
            # A request that passes the fence just before the cutoff
            # must still be sent inside the retention window.
            if native.safety_margin < self.request_deadline:
                raise ValueError(
                    "key safety margin must cover the request deadline"
                )

        for name in ("max_attempts", "max_reopens"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative int")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        if self.compensation_action is not None and (
            type(self.compensation_action) is not str
            or not self.compensation_action.strip()
        ):
            raise ValueError("compensation action must be nonblank")
        if (
            type(self.contract_reference) is not str
            or not self.contract_reference.strip()
        ):
            raise ValueError("contract reference must be documented")

    @property
    def connector_id(self):
        return self.contract.connector_id

    @property
    def version(self):
        return self.contract.version

    def row(self):
        """Column values for safeact_connector_contracts."""

        native = self.contract.native_idempotency
        seconds = lambda value: int(value.total_seconds())  # noqa: E731
        return {
            "connector_id": self.connector_id,
            "version": self.version,
            "side_effect_class": self.side_effect_class.value,
            "native_idempotency": native is not None,
            "key_retention_seconds":
                None if native is None else seconds(native.retention),
            "key_safety_margin_seconds":
                None if native is None else seconds(native.safety_margin),
            "key_binds_parameters":
                None if native is None else native.binds_parameters,
            "key_protects_concurrent":
                None if native is None
                else native.protects_concurrent_same_key,
            "lookup_semantics": self.contract.lookup_semantics.value,
            "request_deadline_seconds": seconds(self.request_deadline),
            "investigation_window_seconds":
                seconds(self.investigation_window),
            "post_escalation_check_interval_seconds":
                seconds(self.post_escalation_check_interval),
            "post_escalation_horizon_seconds":
                seconds(self.post_escalation_horizon),
            "max_attempts": self.max_attempts,
            "max_reopens": self.max_reopens,
            "compensation_action": self.compensation_action,
            "contract_reference": self.contract_reference,
        }


# Column order of safeact_connector_contracts and of
# safeact_connector_contract(); equals the keys of ConnectorProfile.row.
PROFILE_COLUMNS = (
    "connector_id", "version", "side_effect_class",
    "native_idempotency", "key_retention_seconds",
    "key_safety_margin_seconds", "key_binds_parameters",
    "key_protects_concurrent", "lookup_semantics",
    "request_deadline_seconds", "investigation_window_seconds",
    "post_escalation_check_interval_seconds",
    "post_escalation_horizon_seconds", "max_attempts",
    "max_reopens", "compensation_action", "contract_reference",
)


def profile_from_row(row):
    """Rebuild a profile from a safeact_connector_contracts row."""

    values = dict(zip(PROFILE_COLUMNS, row))
    native = None
    if values["native_idempotency"]:
        native = NativeIdempotency(
            key_scope=IdempotencyKeyScope.PROVIDER_ACCOUNT,
            retention=timedelta(seconds=values["key_retention_seconds"]),
            safety_margin=timedelta(
                seconds=values["key_safety_margin_seconds"]
            ),
            protects_concurrent_same_key=values["key_protects_concurrent"],
            binds_parameters=values["key_binds_parameters"],
            contract_reference=values["contract_reference"],
        )

    return ConnectorProfile(
        contract=ConnectorContract(
            connector_id=values["connector_id"],
            version=values["version"],
            native_idempotency=native,
            lookup_semantics=LookupSemantics(values["lookup_semantics"]),
        ),
        side_effect_class=SideEffectClass(values["side_effect_class"]),
        request_deadline=timedelta(
            seconds=values["request_deadline_seconds"]
        ),
        investigation_window=timedelta(
            seconds=values["investigation_window_seconds"]
        ),
        post_escalation_check_interval=timedelta(
            seconds=values["post_escalation_check_interval_seconds"]
        ),
        post_escalation_horizon=timedelta(
            seconds=values["post_escalation_horizon_seconds"]
        ),
        max_attempts=values["max_attempts"],
        max_reopens=values["max_reopens"],
        compensation_action=values["compensation_action"],
        contract_reference=values["contract_reference"],
    )


def payments_profile(
    version="v1", *, lookup=LookupSemantics.STRONG,
    retention=timedelta(hours=24), safety_margin=timedelta(hours=1),
    request_deadline=timedelta(seconds=30),
    investigation_window=timedelta(minutes=30),
    check_interval=timedelta(minutes=15),
    horizon=timedelta(hours=24), max_attempts=3, max_reopens=3,
    limits=None,
):
    """Native key bound to parameters, concurrent-safe, bounded retention."""

    reference = f"experimental mock payments {version}"
    return ConnectorProfile(
        contract=ConnectorContract(
            connector_id=PAYMENTS,
            version=version,
            native_idempotency=NativeIdempotency(
                key_scope=IdempotencyKeyScope.PROVIDER_ACCOUNT,
                retention=retention,
                safety_margin=safety_margin,
                protects_concurrent_same_key=True,
                binds_parameters=True,
                contract_reference=reference,
            ),
            lookup_semantics=lookup,
        ),
        side_effect_class=SideEffectClass.REVERSIBLE_PAYMENT,
        request_deadline=request_deadline,
        investigation_window=investigation_window,
        post_escalation_check_interval=check_interval,
        post_escalation_horizon=horizon,
        max_attempts=max_attempts,
        max_reopens=max_reopens,
        compensation_action="refund_payment",
        contract_reference=reference,
        limits=limits,
    )


def crm_profile(
    version="v1", *, request_deadline=timedelta(seconds=30),
    investigation_window=timedelta(minutes=30),
    check_interval=timedelta(minutes=15),
    horizon=timedelta(hours=24), max_attempts=2, max_reopens=3,
    limits=None,
):
    """No native key; lookup by external reference is stale/incomplete."""

    return ConnectorProfile(
        contract=ConnectorContract(
            connector_id=CRM,
            version=version,
            native_idempotency=None,
            lookup_semantics=LookupSemantics.EVENTUAL,
        ),
        side_effect_class=SideEffectClass.CRM_RECORD,
        request_deadline=request_deadline,
        investigation_window=investigation_window,
        post_escalation_check_interval=check_interval,
        post_escalation_horizon=horizon,
        max_attempts=max_attempts,
        max_reopens=max_reopens,
        compensation_action=None,
        contract_reference=f"experimental mock crm {version}",
        limits=limits,
    )


def email_profile(version="v1", *, request_deadline=timedelta(seconds=30),
                  limits=None):
    """Irreversible, no key, no lookup: ambiguity escalates at once."""

    return ConnectorProfile(
        contract=ConnectorContract(
            connector_id=EMAIL,
            version=version,
            native_idempotency=None,
            lookup_semantics=LookupSemantics.UNAVAILABLE,
        ),
        side_effect_class=SideEffectClass.IRREVERSIBLE_MESSAGE,
        request_deadline=request_deadline,
        investigation_window=timedelta(0),
        post_escalation_check_interval=timedelta(hours=1),
        post_escalation_horizon=timedelta(0),
        max_attempts=1,
        # Reopen only on recorded evidence (a late reply): no lookup.
        max_reopens=1,
        compensation_action=None,
        contract_reference=f"experimental mock email {version}",
        limits=limits,
    )


MOCK_PROFILES = (
    payments_profile("v1"),
    payments_profile("v1-eventual", lookup=LookupSemantics.EVENTUAL),
    crm_profile("v1"),
    email_profile("v1"),
)

# F16 limits (M5 decision 4, 2026-10-20). v1 contracts declare none, so
# their behaviour is unchanged; the v2 contracts carry the limits and are
# what the qualification run uses.
PAYMENTS_LIMITS = ConnectorLimits(
    backoff_base=timedelta(seconds=1), backoff_cap=timedelta(seconds=60),
    lookup_interval=timedelta(seconds=5),
    lookup_interval_cap=timedelta(minutes=5),
    dispatch_rate_per_second=25, dispatch_burst=25,
    breaker_threshold=5, breaker_cooldown=timedelta(seconds=30),
)
SLOW_PROVIDER_LIMITS = ConnectorLimits(
    backoff_base=timedelta(seconds=1), backoff_cap=timedelta(seconds=60),
    lookup_interval=timedelta(seconds=5),
    lookup_interval_cap=timedelta(minutes=5),
    dispatch_rate_per_second=5, dispatch_burst=5,
    breaker_threshold=5, breaker_cooldown=timedelta(seconds=30),
)
LIMITED_PROFILES = (
    payments_profile("v2", limits=PAYMENTS_LIMITS),
    payments_profile("v2-eventual", lookup=LookupSemantics.EVENTUAL,
                     limits=PAYMENTS_LIMITS),
    crm_profile("v2", limits=SLOW_PROVIDER_LIMITS),
    email_profile("v2", limits=SLOW_PROVIDER_LIMITS),
)


# Stripe sandbox: 25 requests/s per account (docs.stripe.com/rate-limits);
# SafeAct stays well below it, and backs off and opens its breaker as for
# the payments mock.
STRIPE_LIMITS = ConnectorLimits(
    backoff_base=timedelta(seconds=1), backoff_cap=timedelta(seconds=60),
    lookup_interval=timedelta(seconds=5),
    lookup_interval_cap=timedelta(minutes=5),
    dispatch_rate_per_second=10, dispatch_burst=10,
    breaker_threshold=5, breaker_cooldown=timedelta(seconds=30),
)


def stripe_profile(
    version="v1", *, request_deadline=timedelta(seconds=30),
    investigation_window=timedelta(minutes=30),
    check_interval=timedelta(minutes=15), horizon=timedelta(hours=24),
    max_attempts=3, max_reopens=3, limits=STRIPE_LIMITS,
):
    """Stripe API v1 refunds in a sandbox, as documented on 2026-10-20.

    Native key: scoped to the account, retained "at least 24 hours" (so
    24 h is the most this contract assumes, less a 1 h margin), compared
    with the original parameters (a mismatch is an idempotency_error),
    and a second request while the first executes gets 409 without
    executing. Lookup: listing refunds is not documented as strongly
    consistent and a refund may still be executing, so absence is never
    proof (EVENTUAL). No compensation: a refund cannot be refunded.
    """

    reference = (
        f"stripe sandbox refunds {version}, Stripe-Version "
        f"{STRIPE_API_VERSION}; docs.stripe.com/api/idempotent_requests, "
        "/error-low-level, /rate-limits, /api/refunds (read 2026-10-20)"
    )
    return ConnectorProfile(
        contract=ConnectorContract(
            connector_id=STRIPE,
            version=version,
            native_idempotency=NativeIdempotency(
                key_scope=IdempotencyKeyScope.PROVIDER_ACCOUNT,
                retention=timedelta(hours=24),
                safety_margin=timedelta(hours=1),
                protects_concurrent_same_key=True,
                binds_parameters=True,
                contract_reference=reference,
            ),
            lookup_semantics=LookupSemantics.EVENTUAL,
        ),
        side_effect_class=SideEffectClass.REVERSIBLE_PAYMENT,
        request_deadline=request_deadline,
        investigation_window=investigation_window,
        post_escalation_check_interval=check_interval,
        post_escalation_horizon=horizon,
        max_attempts=max_attempts,
        max_reopens=max_reopens,
        compensation_action=None,
        contract_reference=reference,
        limits=limits,
    )


STRIPE_PROFILES = (stripe_profile("v1"),)
