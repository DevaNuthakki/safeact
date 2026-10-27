"""Optional observations of the mock HTTP clients; never business evidence.

The observer receives no headers, credentials, raw keys or exception messages.
An observer failure emits a fixed harness error marker and preserves the
underlying client's behavior. Qualification treats that marker as an error.
"""

import hashlib
import json
import os
import uuid


TRACE_ERROR = b"SAFEACT_HTTP_TRACE_ERROR\n"


def correlation_hash(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def payload_hash(value):
    try:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, OverflowError):
        return None
    return hashlib.sha256(data).hexdigest()


class ObservedClient:
    def __init__(self, provider, client, observer):
        self.provider, self.client, self.observer = provider, client, observer

    def _emit(self, event, details):
        try:
            self.observer(event, **details)
        except Exception:
            try:
                os.write(2, TRACE_ERROR)
            except OSError:
                pass

    def _request(self, method, path, **kwargs):
        body = kwargs.get("json") or {}
        params = kwargs.get("params") or {}
        headers = kwargs.get("headers") or {}
        key = (headers.get("Idempotency-Key") or params.get("idempotency_key")
               or params.get("external_ref") or body.get("external_ref")
               or body.get("message_id"))
        details = dict(request_id=uuid.uuid4().hex, provider=self.provider,
                       method=method, path=path, correlation_sha256=correlation_hash(key))
        self._emit("http_start", details)
        try:
            response = getattr(self.client, method.lower())(path, **kwargs)
        except Exception as error:
            self._emit("http_error", dict(details, error_type=type(error).__name__))
            raise
        self._emit("http_response", dict(details, status_code=response.status_code))
        return response

    def post(self, path, **kwargs):
        return self._request("POST", path, **kwargs)

    def get(self, path, **kwargs):
        return self._request("GET", path, **kwargs)

    def close(self):
        self.client.close()
