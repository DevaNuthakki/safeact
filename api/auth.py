"""Sign-in, sign-out and the current principal.

Cookies are described in ``api.cookies``. PostgreSQL stores only
hashes of the session secret and CSRF token and checks them on every
call; the API forwards them and decides nothing itself.
"""

from datetime import datetime

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from api.cookies import (
    CSRF_HEADER, SESSION_COOKIE,
    clear_session_cookies, set_session_cookies,
)
from api.db import transaction
from api.errors import ApiError
from storage import product_api as product


# One fixed body for every failed sign-in (wrong password, unknown user,
# locked account, malformed input), and no request id in it: the
# response must not tell these cases apart.
SIGN_IN_FAILED = {
    "error": {
        "code": "invalid_credentials",
        "message": (
            "Sign-in failed. Check your username and password, "
            "or try again later."
        ),
    }
}

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=1024, repr=False)


class PrincipalView(BaseModel):
    principal_id: str
    tenant_id: str
    username: str
    display_name: str
    kind: str
    roles: list[str]
    demo: bool
    session_expires_at: datetime

    @classmethod
    def of(cls, principal: product.Principal):
        return cls(
            principal_id=str(principal.principal_id),
            tenant_id=principal.tenant_id,
            username=principal.username,
            display_name=principal.display_name,
            kind=principal.kind,
            roles=list(principal.roles),
            demo=principal.demo,
            session_expires_at=principal.expires_at,
        )


class LoginResponse(BaseModel):
    principal: PrincipalView
    csrf_token: str


def session_secret(request: Request):
    return request.cookies.get(SESSION_COOKIE)


def csrf_token(request: Request):
    return request.headers.get(CSRF_HEADER)


@router.post(
    "/login", response_model=LoginResponse,
    responses={401: {"description": "Sign-in failed (one fixed body)."}},
)
def login(body: LoginRequest, request: Request, response: Response):
    settings = request.app.state.settings
    source = request.client.host if request.client else ""

    with transaction(request) as connection:
        result = product.login(
            connection, settings.schema,
            username=body.username, password=body.password, source=source,
        )
        principal = None
        if result.ok:
            principal = product.whoami(
                connection, settings.schema, secret=result.session_secret
            )
    # The failure row is committed by now, so the throttle counts it.

    if not result.ok:
        raise ApiError.fixed(401, SIGN_IN_FAILED)

    set_session_cookies(response, result)
    return LoginResponse(
        principal=PrincipalView.of(principal), csrf_token=result.csrf_token
    )


@router.post("/logout", status_code=204)
def logout(request: Request):
    settings = request.app.state.settings
    with transaction(request) as connection:
        product.logout(
            connection, settings.schema,
            secret=session_secret(request), csrf=csrf_token(request),
        )
    response = Response(status_code=204)
    clear_session_cookies(response)
    return response


@router.get("/me", response_model=PrincipalView)
def me(request: Request):
    settings = request.app.state.settings
    with transaction(request) as connection:
        principal = product.whoami(
            connection, settings.schema, secret=session_secret(request)
        )
    return PrincipalView.of(principal)
