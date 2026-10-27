"""The console's demo directory names exactly the bootstrap's accounts.

The API returns people as principal ids; the demo gives its accounts fixed
ids and the console maps them to names (ui/src/demo/directory.ts). This
keeps the two lists from drifting apart.
"""

import re
import unittest

from pathlib import Path

from deploy.demo.bootstrap import DEMO_ACCOUNTS


DIRECTORY = Path(__file__).resolve().parents[2] / "ui/src/demo/directory.ts"
ENTRY = re.compile(r'"([0-9a-f-]{36})": \{ name: "([^"]+)", title: "([^"]+)" \}')


class DemoDirectoryTests(unittest.TestCase):
    def test_every_demo_account_has_a_fixed_id_and_the_same_name(self):
        entries = {pid: name for pid, name, _ in ENTRY.findall(
            DIRECTORY.read_text(encoding="utf-8"))}
        accounts = {a.principal_id: a.display_name for a in DEMO_ACCOUNTS}
        self.assertNotIn(None, accounts)
        self.assertEqual(len(accounts), len(DEMO_ACCOUNTS))
        self.assertEqual(entries, accounts)

    def test_directory_ids_are_valid_version_4_uuids(self):
        from uuid import UUID

        for account in DEMO_ACCOUNTS:
            with self.subTest(username=account.username):
                self.assertEqual(UUID(account.principal_id).version, 4)


if __name__ == "__main__":
    unittest.main()
