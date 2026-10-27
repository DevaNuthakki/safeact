"""Pure metadata tests for the proposed authority contract."""

import unittest

from storage.authority_schema import (
    authority_metadata,
    actors,
    identities,
    resources,
    grants,
)


class AuthoritySchemaContractTests(unittest.TestCase):

    def test_four_expected_tables(self):
        self.assertEqual(
            set(authority_metadata.tables),
            {
                "safeact_auth_actors",
                "safeact_auth_identities",
                "safeact_auth_resources",
                "safeact_auth_grants",
            },
        )

    def test_all_authority_states_default_disabled(self):
        for table in (actors, identities, resources, grants):
            with self.subTest(table=table.name):
                enabled = table.c.enabled
                self.assertFalse(enabled.nullable)
                self.assertIsNotNone(enabled.server_default)
                self.assertEqual(
                    str(enabled.server_default.arg).lower(),
                    "false",
                )

    def test_identity_has_actor_foreign_key(self):
        referenced = {
            fk.column.table.name
            for fk in identities.foreign_keys
        }
        self.assertEqual(
            referenced, {"safeact_auth_actors"}
        )

    def test_grant_references_actor_and_resource(self):
        referenced = {
            fk.column.table.name
            for fk in grants.foreign_keys
        }
        self.assertEqual(
            referenced,
            {
                "safeact_auth_actors",
                "safeact_auth_resources",
            },
        )

    def test_resource_owner_is_unique(self):
        names = {
            constraint.name
            for constraint in resources.constraints
        }
        self.assertIn(
            "uq_safeact_auth_resources_owner",
            names,
        )


if __name__ == "__main__":
    unittest.main()
