"""One-shot bootstrap of the Compose stack (DEMO ONLY).

    python -m deploy.docker.bootstrap --credentials /state/demo-credentials

The documented sequence after a migration (CLAUDE.md), inside the stack:
1. ``alembic upgrade head``;
2. deploy/security/provision_local_admission_role.sql in one transaction,
   as ``psql -1 -v ON_ERROR_STOP=1`` runs it (the file has no psql
   meta-commands; it checks the head revision itself);
3. the demo rebuild (deploy/demo/bootstrap.py): safeact_demo with fresh role
   passwords, sign-ins and agent keys, written mode 600 to the credentials
   file on the stack's state volume.
Every service waits for this one to finish successfully.
"""

import argparse
import subprocess
import sys

from pathlib import Path

import psycopg

from deploy.demo import bootstrap as demo
from storage.database import local_admin_settings


ROOT = Path(__file__).resolve().parents[2]
PROVISION = ROOT / "deploy/security/provision_local_admission_role.sql"


def provision(path=PROVISION):
    script = path.read_text(encoding="utf-8")
    if any(line.lstrip().startswith("\\") for line in script.splitlines()):
        raise RuntimeError("psql meta-commands are not supported here")
    # No parameters: psycopg sends the whole script as one simple query,
    # inside one transaction that commits only if every statement succeeds.
    with psycopg.connect(**local_admin_settings()) as connection:
        connection.execute(script)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--credentials", type=Path, required=True)
    args = parser.parse_args(argv)
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                   cwd=ROOT, check=True)
    provision()
    return demo.main(["--credentials", str(args.credentials)])


if __name__ == "__main__":
    sys.exit(main())
