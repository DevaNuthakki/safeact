"""The Stripe sandbox key: a restricted test-mode key in a file, never shown.

The author creates the key and the file themselves (default
``~/.config/safeact/stripe.key``, or ``SAFEACT_STRIPE_KEY_FILE``). It is
refused unless the file:
- resolves outside the repository;
- is a regular file (not a symbolic link) owned by the current user;
- is readable by that user only (no group or other permission bits);
- holds exactly one restricted test-mode key (``rk_test_``).

Every other Stripe key kind is refused by prefix, before anything is
sent: a live key (``sk_live_``, ``rk_live_``), an unrestricted test key
(``sk_test_``: the author asked for a restricted key with three
permissions), a publishable key (``pk_``) and a webhook secret
(``whsec_``).

The value is held in a StripeKey whose repr and str never contain it.
Only the adapter reveals it, to build the Authorization header. No error
message ever includes it, or even its length or a prefix beyond the kind.

A call to Stripe is allowed only with SAFEACT_ALLOW_STRIPE_SANDBOX=1 and
never in CI (CI or GITHUB_ACTIONS set).
"""

import os
import re
import stat

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KEY_FILE = Path("~/.config/safeact/stripe.key")
KEY_FILE_ENV = "SAFEACT_STRIPE_KEY_FILE"
SANDBOX_ENV = "SAFEACT_ALLOW_STRIPE_SANDBOX"

ACCEPTED = re.compile(r"^rk_test_[A-Za-z0-9]{16,247}$")
REFUSED_KINDS = (
    ("sk_live_", "a live secret key"),
    ("rk_live_", "a live restricted key"),
    ("pk_live_", "a live publishable key"),
    ("sk_test_", "an unrestricted test key (use a restricted rk_test_ key)"),
    ("pk_test_", "a publishable key"),
    ("whsec_", "a webhook signing secret"),
    ("sk_org_", "an organization key"),
)


class StripeKeyRefused(RuntimeError):
    """The key or its file failed a safety check (never contains the key)."""


class StripeSandboxRefused(RuntimeError):
    """A Stripe call is not allowed in this environment."""


class StripeKey:
    __slots__ = ("_value",)

    def __init__(self, value):
        check_key_kind(value)
        self._value = value

    def reveal(self):
        return self._value

    def __repr__(self):
        return "StripeKey(rk_test_<hidden>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("A Stripe key cannot be pickled")


def check_key_kind(value):
    if type(value) is not str:
        raise StripeKeyRefused("The Stripe key must be text")
    for prefix, kind in REFUSED_KINDS:
        if value.startswith(prefix):
            raise StripeKeyRefused(f"Refused {kind}")
    if not ACCEPTED.fullmatch(value):
        raise StripeKeyRefused(
            "The file does not hold one restricted test-mode key (rk_test_)"
        )


def key_file_path(env=None):
    env = os.environ if env is None else env
    return Path(env.get(KEY_FILE_ENV) or DEFAULT_KEY_FILE).expanduser()


def load_stripe_key(env=None, *, repo_root=ROOT):
    path = key_file_path(env)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise StripeKeyRefused(f"No Stripe key file at {path}") from None
    if stat.S_ISLNK(info.st_mode):
        raise StripeKeyRefused("The key file must not be a symbolic link")
    resolved = path.resolve(strict=True)
    if resolved.is_relative_to(Path(repo_root).resolve()):
        raise StripeKeyRefused("The key file must live outside the repository")
    if not stat.S_ISREG(info.st_mode):
        raise StripeKeyRefused("The key file must be a regular file")
    if info.st_uid != os.getuid():
        raise StripeKeyRefused("The key file must be owned by the current user")
    if info.st_mode & 0o077:
        raise StripeKeyRefused("The key file must be mode 600 (owner only)")
    return StripeKey(resolved.read_text("utf-8").strip())


def sandbox_allowed(env=None):
    env = os.environ if env is None else env
    return (
        env.get(SANDBOX_ENV) == "1"
        and not env.get("CI")
        and not env.get("GITHUB_ACTIONS")
    )


def require_sandbox_allowed(env=None):
    if not sandbox_allowed(env):
        raise StripeSandboxRefused(
            "Stripe calls need SAFEACT_ALLOW_STRIPE_SANDBOX=1 and never run in CI"
        )
