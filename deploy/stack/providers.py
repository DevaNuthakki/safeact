"""The mock providers' HTTP wire APIs as their own process (DEMO ONLY).

    python -m deploy.stack.providers [--plain] [--host 127.0.0.1]

Serves payments, CRM and email on 8101, 8102, 8103 and the harness-only
admin API (ledger, fault switches, held calls) on 8109, by default with
the demo's fixed fault script (deploy/demo/run_stack.SCRIPT); ``--plain``
serves mocks that behave honestly until the admin API sets a fault.
Mocks only; nothing reaches a real provider.
"""

import argparse
import signal
import sys
import threading

from connectors.http_mock.servers import MockProviderServers
from connectors.ledger import EffectsLedger
from deploy.stack.config import DEFAULT_PROVIDER_PORTS


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plain", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    for kind, port in DEFAULT_PROVIDER_PORTS.items():
        parser.add_argument(f"--{kind}-port", type=int, default=port)
    args = parser.parse_args(argv)

    ledger = EffectsLedger()
    providers = None
    if not args.plain:
        from deploy.demo.run_stack import (
            ScriptedCrm, ScriptedEmail, ScriptedPayments,
        )
        providers = (
            ScriptedPayments(ledger=ledger), ScriptedCrm(ledger=ledger),
            ScriptedEmail(ledger=ledger),
        )
    servers = MockProviderServers(
        ledger=ledger, providers=providers, host=args.host,
        ports={kind: getattr(args, f"{kind}_port")
               for kind in DEFAULT_PROVIDER_PORTS},
    ).start()
    print("mock providers ready " + " ".join(
        f"{kind}={port}" for kind, port in servers.ports.items()
    ), flush=True)

    stopping = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    stopping.wait()
    servers.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
