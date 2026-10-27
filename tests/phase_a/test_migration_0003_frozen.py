"""Migration 0003 is pushed and frozen: its rendered SQL must not drift.

The golden files were captured from the builder before it was moved
into ``storage.lifecycle_sql.v0003``. Any change to lifecycle states,
transition tables, enums or shared SQL helpers that would alter what
0003 creates on a fresh database fails here. Schema changes belong in
a new migration.
"""

import unittest

from hashlib import sha256
from pathlib import Path

from storage.lifecycle_sql import v0003 as frozen


FIXTURES = Path(__file__).resolve().parent / "fixtures"
SEPARATOR = "\n-- safeact:statement --\n"
DISPOSABLE = "safeact_it_" + "0" * 32

DIGESTS = {
    "upgrade_public": (
        "0703121a3fe78b26e522637ce97c62e6"
        "b1dc12c5fcc85b3b18d32a83b00face7"
    ),
    "downgrade_public": (
        "87696545c5238a021ac9373aa2ed044a"
        "d11c5a081f9f58d2fd405cb9eff2d307"
    ),
    "grants_disposable": (
        "cc07f5b7dbf47882272d0f0bca5ec62a"
        "b43cbddf8f1a79fea19ecbd1ca9bd8ef"
    ),
}


def render(statements):
    return SEPARATOR.join(s.as_string(None) for s in statements)


def golden(name):
    text = (FIXTURES / f"migration_0003_{name}.sql").read_text("utf-8")
    return text[:-1] if text.endswith("\n") else text


class Migration0003FrozenTests(unittest.TestCase):

    def assert_frozen(self, name, rendered):
        self.assertEqual(
            sha256(golden(name).encode()).hexdigest(), DIGESTS[name],
            f"golden file {name} was edited",
        )
        self.assertEqual(rendered, golden(name))
        self.assertEqual(
            sha256(rendered.encode()).hexdigest(), DIGESTS[name]
        )

    def test_public_upgrade_is_byte_identical(self):
        self.assert_frozen(
            "upgrade_public", render(frozen.upgrade_statements("public"))
        )

    def test_public_downgrade_is_byte_identical(self):
        self.assert_frozen(
            "downgrade_public",
            render(frozen.downgrade_statements("public")),
        )

    def test_disposable_schema_renders_the_same_sql(self):
        for build, name in (
            (frozen.upgrade_statements, "upgrade_public"),
            (frozen.downgrade_statements, "downgrade_public"),
        ):
            self.assertEqual(
                render(build(DISPOSABLE)),
                golden(name).replace('"public"', f'"{DISPOSABLE}"'),
            )

    def test_disposable_grants_are_byte_identical(self):
        self.assert_frozen("grants_disposable", render(
            frozen.disposable_grant_statements(
                DISPOSABLE,
                owner="safeact_fn_golden",
                workers=("safeact_wk_golden_a", "safeact_wk_golden_b"),
                recoverers=("safeact_rc_golden",),
            )
        ))


if __name__ == "__main__":
    unittest.main()
