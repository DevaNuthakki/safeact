"""A small Toxiproxy client (harness only; Toxiproxy 2.12.0 REST API).

Proxies sit between a system under test and the HTTP mock providers, so
the network itself can be slowed, cut, reset or taken down at a chosen
moment: the "Toxiproxy for network behavior" half of blueprint §08 (the
other half, commit and acknowledgement boundaries, uses deterministic
failpoints). No new dependency: httpx2 is already pinned.

Toxics used by the qualification scenarios:
  latency       {latency, jitter} ms added to every chunk
  timeout       {timeout} ms: data stops, the connection closes after it
                (0: never closes; the caller times out)
  reset_peer    {timeout} ms then TCP RST
  slow_close    {delay} ms before closing
  limit_data    {bytes}: close after this many bytes
  down          the proxy refuses connections (enabled=false)
"""

import os

from dataclasses import dataclass

import httpx2 as httpx


API = os.environ.get("SAFEACT_TOXIPROXY_URL", "http://127.0.0.1:8474")
LISTEN_HOST = "0.0.0.0"
UPSTREAM_HOST = os.environ.get("SAFEACT_TOXIPROXY_UPSTREAM_HOST",
                               "host.docker.internal")
TOXICS = frozenset({"latency", "timeout", "reset_peer", "slow_close",
                    "limit_data", "bandwidth"})


class ToxiproxyError(RuntimeError):
    pass


@dataclass(frozen=True)
class Proxy:
    name: str
    listen_port: int
    upstream_port: int

    @property
    def url(self):
        return f"http://127.0.0.1:{self.listen_port}"


class Toxiproxy:

    def __init__(self, api=API, *, upstream_host=UPSTREAM_HOST):
        self._http = httpx.Client(base_url=api, timeout=5, trust_env=False)
        self.upstream_host = upstream_host

    def close(self):
        self._http.close()

    def _check(self, response, *ok):
        if response.status_code not in (ok or (200, 201, 204)):
            raise ToxiproxyError(
                f"{response.request.method} {response.request.url.path}: "
                f"{response.status_code} {response.text[:200]}")
        return response

    def version(self):
        return self._check(self._http.get("/version")).json()["version"]

    def create(self, name, listen_port, upstream_port):
        self._http.delete(f"/proxies/{name}")
        self._check(self._http.post("/proxies", json={
            "name": name, "listen": f"{LISTEN_HOST}:{listen_port}",
            "upstream": f"{self.upstream_host}:{upstream_port}",
            "enabled": True,
        }), 201)
        return Proxy(name, listen_port, upstream_port)

    def delete(self, name):
        self._check(self._http.delete(f"/proxies/{name}"), 204, 404)

    def toxic(self, proxy, kind, *, stream="downstream", toxicity=1.0,
              **attributes):
        if kind not in TOXICS:
            raise ValueError(f"unknown toxic {kind!r}")
        name = f"{kind}_{stream}"
        self._check(self._http.post(f"/proxies/{proxy}/toxics", json={
            "name": name, "type": kind, "stream": stream,
            "toxicity": toxicity, "attributes": attributes,
        }))
        return name

    def remove_toxic(self, proxy, name):
        self._check(self._http.delete(f"/proxies/{proxy}/toxics/{name}"),
                    204, 404)

    def clear(self, proxy):
        toxics = self._check(self._http.get(f"/proxies/{proxy}/toxics")).json()
        for toxic in toxics:
            self.remove_toxic(proxy, toxic["name"])
        self.enable(proxy, True)

    def enable(self, proxy, enabled):
        self._check(self._http.post(f"/proxies/{proxy}",
                                    json={"enabled": enabled}))
