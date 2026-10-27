"""Phase A runs the walkthrough preparation helper's rerun tests.

The helper and its tests live in ui/scripts (frontend-owned, outside the
pinned inventory). Phase A is the suite CI already runs with the API and MCP
locks installed, so the tests are re-exported here rather than left to a
manual command. They use mocks and a temporary directory only.
"""

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "ui" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from test_prepare_walkthrough import PreparationRerunTests  # noqa: E402,F401
