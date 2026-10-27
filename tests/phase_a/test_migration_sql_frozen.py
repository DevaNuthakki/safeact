"""Committed migrations from 0004 on are frozen: rendered SQL is pinned.

Each digest is recorded in the commit that adds its migration and is
never updated afterwards. A mismatch means a committed migration's SQL
changed (directly, or through a helper it imports): put the change in a
new migration instead. 0003 has its own byte-level test.
"""

import importlib
import unittest

from hashlib import sha256

from storage import durable_lifecycle_sql as lsql


SEPARATOR = "\n-- safeact:statement --\n"
DISPOSABLE = "safeact_it_" + "0" * 32

DIGESTS = {
    "v0004": {
        "upgrade": "fcd23ab6d048114bbe67d7ebb5c33523"
                   "c5c7bcc374d4fcb8190a3a9189840ca2",
        "downgrade": "895d725df1f8b55adbc533d9be95c8b3"
                     "bc80edd3e5666870dfc7e6be1bd41e0e",
    },
    "v0005": {
        "upgrade": "14cd6f7f5f78582208e49dd26f5c6b9c"
                   "461b0526666291a279d64f4e7e5da719",
        "downgrade": "29c1c736f25c37fd63d04b866d2926e2"
                     "45799f857d58fc813f93af7b6cc180da",
    },
    "v0006": {
        "upgrade": "baf1ce5a23c89876b7fe60ca7163234c"
                   "a9003c4862bd666227297128c1492553",
        "downgrade": "78301c209e3698c2e478a035937a7759"
                     "af9e9982631aa06949a32bc5f84c1509",
    },
    "v0007": {
        "upgrade": "3569f4e585c3ad0e083097ff18ee766c"
                   "a5f71895596fdee527390046ac8afc42",
        "downgrade": "d5c9d68879e6899fd9aa4b6223304f58"
                     "62d672c9c4fef75e92afdf6b5c64eddd",
    },
    "v0008": {
        "upgrade": "3e06139677bbba8840aace344a63dd31"
                   "79ac309a9b84aa5ebf3b2bed1500d544",
        "downgrade": "5d7c1977d59d073d1d888f13d83115f9"
                     "8e452384ed768872c007c2bbe9378920",
    },
    "v0009": {
        "upgrade": "3900f086b15a170ec0de759e9e2d056e"
                   "52f83423c67b33f60b8944dd78e6b0e5",
        "downgrade": "22971b06c4e0b03d65ca217500c5a6ea"
                     "ff690cdb3a14ac1a5bc0cf54c6354dc0",
    },
    "v0010": {
        "upgrade": "0f080c9209cd0dd4f17395118669c2a7"
                   "0b93a7013ae3946bc03985dc46c47d04",
        "downgrade": "6fc01a9fef21a72c217ecf675b46b18e"
                     "cfd469ab16daaf9cd7c8c15a7d7cb570",
    },
    "v0011": {
        "upgrade": "6584f4a2c53f6c9ff23401809a28f1a5"
                   "537959575cc16aab890008146348d37b",
        "downgrade": "7dc10d9087fc322d4c62f1c95ee64443"
                     "395e6150918b4e2478df951e26be7e57",
    },
    "v0012": {
        "upgrade": "d9d18e46abf7359f90a231816332d3e4"
                   "4690aa81b8a04112ba9723cb560a34dd",
        "downgrade": "b5472bdadcde297df39ab189b5355711"
                     "71b156201d799950c5f3acc6978cca14",
    },
    "v0013": {
        "upgrade": "ab009f09e0f1234e9fef660a6bc9cd82"
                   "b7045e58898489c18f23e8ce3a74970f",
        "downgrade": "5468683a1bc2cdce65ad4310f94c8e7b"
                     "6414202e2d3f4a07f42de2b74e32c0ca",
    },
    "v0014": {
        "upgrade": "be1b45cb1e2ccd3f6efd62f89bcceec2"
                   "60834b7d1739a067cf3ee74288e978fb",
        "downgrade": "abaecfdd4f3881ccdba323911a4c90f5"
                     "acb593c62f847e4e3f8af71945e00224",
    },
    "v0015": {
        "upgrade": "8d1c50c9fe1c6fe6c7291cc8bbde3bd4"
                   "b06fa1c88542d3c4e90c841bb8da2ed3",
        "downgrade": "3e4e4f12cb6128e07df29f82c196dc26"
                     "197e2cb208fddf57dc4b69bc80f90fe7",
    },
    "v0016": {
        "upgrade": "ba3df928e8006320eb83b2570257b763"
                   "5bd32a9634d8c5fddaca80961de4e520",
        "downgrade": "424c62492587ef1c9cfbf9f09bf9f40f"
                     "9ab4a7ae991a64ddcded7eaeae50e07e",
    },
    "v0017": {
        "upgrade": "3cf677866874e470aae22cb2f901c576"
                   "f711e3745c81e0fe38fe96f40d2b4889",
        "downgrade": "4b27a70f5b6b293fd2a40d1c74ac2e4f"
                     "0d29da822b0372605bb4e7ec51695548",
    },
    "v0018": {
        "upgrade": "f792c3a973133dac4c41dba5947c016e"
                   "24f44610c7817b84b733df2ec7db5626",
        "downgrade": "1a972089b42543c995f4c8c3bb2e3c3e"
                     "b740179e9b96acd8c495d37c5e8bb058",
    },
    "v0019": {
        "upgrade": "ab26bea861c4f9490d414dbf54f09d03"
                   "67a130d03dd7dd19617979048f7bcec9",
        "downgrade": "4291151d86ec1b2651ae730159dac0e0"
                     "e73d0689f787c50e86e6e78b5c1f9fd3",
    },
    "v0020": {
        "upgrade": "17eff082a84cfa78ec50e1d8fef0c794"
                   "25e202018653cd741677bb23e31f7f78",
        "downgrade": "5e0a8b9bcd977796d32b30ea71f9cffc"
                     "47fea45f53cdb50d5a75c2e540a652bc",
    },
    "v0021": {
        "upgrade": "d06931d233c0995336eea7f18fdb140c"
                   "a2c33dab3f164a5ef74aa56fb677556d",
        "downgrade": "fcba7285ef8082e4eba6d17087a9dad7"
                     "ef9955c1c4e6b5e714414feeef0e5b24",
    },
    "v0022": {
        "upgrade": "34bbf38604969570f425f7e54be84f8b"
                   "3ad414053195a0e4dc9e878b7203740a",
        "downgrade": "d782226e710dccace79538de208cbb2a"
                     "e8210d02969cf0f9219b8b9306fcf50a",
    },
}


def render(statements):
    return SEPARATOR.join(s.as_string(None) for s in statements)


class FrozenMigrationSqlTests(unittest.TestCase):

    def test_every_later_migration_is_pinned(self):
        self.assertEqual(
            {m.__name__.rsplit(".", 1)[1] for m in lsql.MIGRATIONS[1:]},
            set(DIGESTS),
        )

    def test_rendered_sql_matches_recorded_digest(self):
        for name, digests in DIGESTS.items():
            module = importlib.import_module(f"storage.lifecycle_sql.{name}")
            for kind, digest in digests.items():
                with self.subTest(migration=name, kind=kind):
                    build = getattr(module, f"{kind}_statements")
                    public = render(build("public"))
                    self.assertEqual(
                        sha256(public.encode()).hexdigest(), digest
                    )
                    self.assertEqual(
                        render(build(DISPOSABLE)),
                        public.replace('"public"', f'"{DISPOSABLE}"'),
                    )

    def test_migration_files_use_their_frozen_module(self):
        for module in lsql.MIGRATIONS[1:]:
            with self.subTest(revision=module.REVISION):
                path = (
                    lsql.ROOT / "deploy" / "migrations" / "versions"
                    / f"{module.REVISION}.py"
                )
                text = path.read_text("utf-8")
                self.assertIn(f"from {module.__name__} import", text)
                self.assertIn(f'revision = "{module.REVISION}"', text)


if __name__ == "__main__":
    unittest.main()
