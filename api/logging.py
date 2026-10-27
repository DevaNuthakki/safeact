"""Structured JSON logs with secret redaction.

Every record is one JSON object. Values under sensitive keys (password,
secret, token, csrf, cookie, authorization, session) are replaced, and
``key=value`` secrets inside free text are masked, so credentials and
session material never reach a log line even by accident.
"""

import json
import logging
import re
import time

from collections.abc import Mapping


REDACTED = "[redacted]"
SENSITIVE_KEY = re.compile(
    r"pass(word)?|secret|token|csrf|cookie|authori[sz]ation|session|"
    r"credential|api[_-]?key",
    re.IGNORECASE,
)
INLINE_SECRET = re.compile(
    r"(?i)\b(pass(?:word)?|secret|token|csrf|session|api[_-]?key)"
    r"(\s*[=:]\s*)([^\s,;&]+)"
)
URL_PASSWORD = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^:/\s@]*:)([^@\s]+)(@)")
# Key-shaped values wherever they appear: SafeAct agent keys and
# OpenAI-style API keys.
KEY_SHAPED = re.compile(
    r"\b(?:sak_[0-9A-Za-z]{8,}|sk-[0-9A-Za-z_-]{8,}"
    # Stripe API keys and webhook secrets (M5.3).
    r"|(?:sk|rk|pk)_(?:test|live|org)_[0-9A-Za-z]{8,}|whsec_[0-9A-Za-z]{8,})"
)

_RECORD_FIELDS = frozenset(vars(logging.makeLogRecord({}))) | {"message"}


def redact_text(text):
    text = URL_PASSWORD.sub(r"\1" + REDACTED + r"\3", text)
    text = KEY_SHAPED.sub(REDACTED, text)
    return INLINE_SECRET.sub(r"\1\2" + REDACTED, text)


def redact(value, _depth=0):
    if _depth > 8:
        return REDACTED
    if isinstance(value, Mapping):
        return {
            str(k): (
                REDACTED if SENSITIVE_KEY.search(str(k))
                else redact(v, _depth + 1)
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(v, _depth + 1) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))


class JsonFormatter(logging.Formatter):

    def format(self, record):
        payload = {
            "ts": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)
            ) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _RECORD_FIELDS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), default=str, sort_keys=True)


def configure(level=logging.INFO):
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("safeact")
    logger.handlers[:] = [handler]
    logger.setLevel(level)
    logger.propagate = False
    return logger
