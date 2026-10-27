"""Write the API's OpenAPI schema for the typed console client.

    .venv/bin/python -m deploy.demo.export_openapi [--check]

The schema comes from the FastAPI app itself (no database needed) and
is written with sorted keys so the file only changes when the API does.
``--check`` fails if the committed file is out of date (CI).
"""

import argparse
import json
import sys
from pathlib import Path

from api.app import create_app
from api.settings import Settings


ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "ui" / "src" / "api" / "openapi.json"


def schema_text():
    app = create_app(Settings(
        schema="safeact_demo", role="safeact_demo_api", password="unused",
    ), connect=lambda: None)
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    text = schema_text()
    if args.check:
        if not TARGET.is_file() or TARGET.read_text("utf-8") != text:
            print("ui/src/api/openapi.json is out of date: "
                  "run python -m deploy.demo.export_openapi", file=sys.stderr)
            return 1
        return 0
    TARGET.write_text(text, encoding="utf-8")
    print(f"Wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
