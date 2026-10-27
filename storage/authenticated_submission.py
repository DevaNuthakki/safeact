"""Token-verified entry point for SafeAct durable admission.

This service must be constructed by trusted application code.
It is not an HTTP server, process sandbox, or authorization
boundary against other Python code in the same process.
"""

from core.admission_authorization import (
    AdmissionDenied,
    RequestedIntent,
)
from core.authority_registry import AuthorityRegistry
from core.verified_identity import IdentityVerifier
from storage.authorized_submission import (
    observe_scoped_intent,
    submit_scoped_intent,
)


class AuthenticatedAdmissionConfigurationError(ValueError):
    """Invalid trusted admission-service dependencies."""


class AuthenticatedAdmissionService:
    """Join verification, authority resolution, and admission.

    Public methods accept only a raw token and RequestedIntent.
    Actor identities and permission grants are never accepted
    as arguments to those methods.

    Authentication and authority resolution occur before
    invoking the existing scoped-submission adapter.
    That adapter enforces exact-scope authorization before SQL.
    """

    __slots__ = (
        "_engine",
        "_identity_verifier",
        "_authority_registry",
    )

    def __init__(
        self,
        *,
        engine,
        identity_verifier: IdentityVerifier,
        authority_registry: AuthorityRegistry,
    ):
        if (
            engine is None
            or type(identity_verifier) is not IdentityVerifier
            or type(authority_registry) is not AuthorityRegistry
        ):
            raise AuthenticatedAdmissionConfigurationError(
                "Invalid trusted admission dependencies"
            )

        self._engine = engine
        self._identity_verifier = identity_verifier
        self._authority_registry = authority_registry

    def _resolve(self, token: str, request: RequestedIntent):
        if type(request) is not RequestedIntent:
            raise AdmissionDenied(
                "A valid requested intent is required"
            )

        principal = self._identity_verifier.verify(token)

        return self._authority_registry.resolve_verified(
            principal
        )

    def submit(
        self,
        *,
        token: str,
        request: RequestedIntent,
    ):
        actor, grants = self._resolve(token, request)

        return submit_scoped_intent(
            self._engine,
            actor,
            grants,
            request,
        )

    def observe(
        self,
        *,
        token: str,
        request: RequestedIntent,
    ):
        actor, grants = self._resolve(token, request)

        return observe_scoped_intent(
            self._engine,
            actor,
            grants,
            request,
        )
