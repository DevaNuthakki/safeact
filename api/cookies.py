"""Session cookies.

The browser holds two cookies, both ``Secure`` with the ``__Host-``
prefix (no Domain, Path=/), and SameSite=Strict:

- ``__Host-safeact_session``: the session secret. HttpOnly, so page
  scripts never see it.
- ``__Host-safeact_csrf``: the CSRF token, readable by the console so
  it can send it back in the ``X-SafeAct-CSRF`` header on every write.
  Another site can neither read it nor set that header.

Browsers treat http://localhost as a secure context, so Secure cookies
work in local development too.
"""

from datetime import datetime, timezone


SESSION_COOKIE = "__Host-safeact_session"
CSRF_COOKIE = "__Host-safeact_csrf"
CSRF_HEADER = "X-SafeAct-CSRF"


def set_session_cookies(response, result):
    max_age = max(
        0, int((result.expires_at - datetime.now(timezone.utc)).total_seconds())
    )
    common = {"max_age": max_age, "path": "/", "secure": True,
              "samesite": "strict"}
    response.set_cookie(
        SESSION_COOKIE, result.session_secret, httponly=True, **common
    )
    response.set_cookie(
        CSRF_COOKIE, result.csrf_token, httponly=False, **common
    )


def clear_session_cookies(response):
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(
            name, path="/", secure=True, samesite="strict",
            httponly=name == SESSION_COOKIE,
        )
