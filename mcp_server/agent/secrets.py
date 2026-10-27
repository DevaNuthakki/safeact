"""The OpenAI API key: read from a file outside the repository, never shown.

The key lives in a file the author creates (default
``~/.config/safeact/openai.key``, or ``SAFEACT_OPENAI_KEY_FILE``). It is
refused unless the file resolves outside the repository, is a regular
file owned by the current user, and is readable by that user only (no
group or other permission bits). The value is held in an ApiKey whose
repr and str never contain it; only the model adapter reveals it, to the
SDK client constructor. No error message ever includes it.

A paid model call is allowed only with SAFEACT_ALLOW_PAID_MODEL=1 and
never in CI (CI or GITHUB_ACTIONS set).
"""

import os
import re
import stat

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KEY_FILE = Path("~/.config/safeact/openai.key")
KEY_FILE_ENV = "SAFEACT_OPENAI_KEY_FILE"
PAID_ENV = "SAFEACT_ALLOW_PAID_MODEL"
SHAPE = re.compile(r"^[A-Za-z0-9_\-]{20,512}$")


class KeyFileRefused(RuntimeError):
    """The key file failed a safety check (the message never has the key)."""


class PaidModelRefused(RuntimeError):
    """A paid model call is not allowed in this environment."""


class ApiKey:
    __slots__ = ("_value",)

    def __init__(self, value):
        self._value = value

    def reveal(self):
        return self._value

    def __repr__(self):
        return "ApiKey(<hidden>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("An API key cannot be pickled")


def key_file_path(env=None):
    env = os.environ if env is None else env
    return Path(env.get(KEY_FILE_ENV) or DEFAULT_KEY_FILE).expanduser()


def load_openai_key(env=None, *, repo_root=ROOT):
    path = key_file_path(env)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise KeyFileRefused(f"No key file at {path}") from None
    if stat.S_ISLNK(info.st_mode):
        raise KeyFileRefused("The key file must not be a symbolic link")
    resolved = path.resolve(strict=True)
    if resolved.is_relative_to(Path(repo_root).resolve()):
        raise KeyFileRefused("The key file must live outside the repository")
    if not stat.S_ISREG(info.st_mode):
        raise KeyFileRefused("The key file must be a regular file")
    if info.st_uid != os.getuid():
        raise KeyFileRefused("The key file must be owned by the current user")
    if info.st_mode & 0o077:
        raise KeyFileRefused("The key file must be mode 600 (owner only)")
    value = resolved.read_text("utf-8").strip()
    if not SHAPE.fullmatch(value):
        raise KeyFileRefused("The key file does not hold one API key")
    return ApiKey(value)


def paid_model_allowed(env=None):
    env = os.environ if env is None else env
    return (
        env.get(PAID_ENV) == "1"
        and not env.get("CI")
        and not env.get("GITHUB_ACTIONS")
    )


def require_paid_model_allowed(env=None):
    if not paid_model_allowed(env):
        raise PaidModelRefused(
            "Paid model calls need SAFEACT_ALLOW_PAID_MODEL=1 and never run in CI"
        )
