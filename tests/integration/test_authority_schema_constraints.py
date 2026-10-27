"""Authority schema tests using disposable PostgreSQL schemas.

Never creates persistent SafeAct authorization tables.
"""

from contextlib import contextmanager
import os
import unittest
from uuid import uuid4

import sqlalchemy as sa

from storage.database import local_database_url
from storage.authority_schema import (
    authority_metadata,
    actors,
    identities,
    resources,
    grants,
)


@unittest.skipUnless(
    os.environ.get("SAFEACT_RUN_DB_INTEGRATION") == "1"
    and os.environ.get("SAFEACT_RUN_DISPOSABLE_DB_TESTS") == "1",
    "Disposable PostgreSQL tests not enabled",
)
class AuthoritySchemaConstraintTests(unittest.TestCase):

    def setUp(self):
        self.schema = "safeact_it_" + uuid4().hex
        self.engine = sa.create_engine(
            local_database_url(),
            pool_pre_ping=True,
        )

        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.cleanup_schema)

        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                f'CREATE SCHEMA "{self.schema}"'
            )

            scoped = connection.execution_options(
                schema_translate_map={None: self.schema}
            )

            authority_metadata.create_all(scoped)

    def cleanup_schema(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'
            )

    @contextmanager
    def transaction(self):
        with self.engine.begin() as connection:
            yield connection.execution_options(
                schema_translate_map={None: self.schema}
            )

    def seed(self):
        with self.transaction() as connection:
            connection.execute(sa.insert(actors).values(
                tenant_id="tenant-a",
                actor_id="actor-a",
                enabled=True,
            ))

            connection.execute(sa.insert(identities).values(
                issuer="https://id.example",
                subject="subject-a",
                tenant_id="tenant-a",
                actor_id="actor-a",
                enabled=True,
            ))

            connection.execute(sa.insert(resources).values(
                tenant_id="tenant-a",
                resource_id="resource-a",
                connector_id="connector-a",
                provider_account_id="account-a",
                enabled=True,
            ))

            connection.execute(sa.insert(grants).values(
                tenant_id="tenant-a",
                actor_id="actor-a",
                resource_id="resource-a",
                connector_id="connector-a",
                provider_account_id="account-a",
                action_type="create_case",
                contract_version="v1",
                enabled=True,
            ))

    def test_defaults_are_disabled(self):
        with self.transaction() as connection:
            connection.execute(sa.insert(actors).values(
                tenant_id="tenant-a",
                actor_id="actor-a",
            ))

            enabled = connection.execute(
                sa.select(actors.c.enabled)
            ).scalar_one()

            self.assertIs(enabled, False)

    def test_complete_authority_chain_inserts(self):
        self.seed()

        with self.transaction() as connection:
            for table in (actors, identities, resources, grants):
                with self.subTest(table=table.name):
                    self.assertEqual(
                        connection.execute(
                            sa.select(sa.func.count()).select_from(table)
                        ).scalar_one(),
                        1,
                    )

    def test_identity_requires_existing_actor(self):
        with self.assertRaises(sa.exc.IntegrityError):
            with self.transaction() as connection:
                connection.execute(sa.insert(identities).values(
                    issuer="https://id.example",
                    subject="subject-a",
                    tenant_id="tenant-a",
                    actor_id="missing-actor",
                    enabled=True,
                ))

    def test_single_identity_binding_per_actor(self):
        self.seed()

        with self.assertRaises(sa.exc.IntegrityError):
            with self.transaction() as connection:
                connection.execute(sa.insert(identities).values(
                    issuer="https://another-id.example",
                    subject="another-subject",
                    tenant_id="tenant-a",
                    actor_id="actor-a",
                    enabled=True,
                ))

    def test_grant_requires_existing_actor(self):
        self.seed()

        with self.assertRaises(sa.exc.IntegrityError):
            with self.transaction() as connection:
                connection.execute(sa.insert(grants).values(
                    tenant_id="tenant-a",
                    actor_id="missing-actor",
                    resource_id="resource-a",
                    connector_id="connector-a",
                    provider_account_id="account-a",
                    action_type="create_case",
                    contract_version="v1",
                    enabled=True,
                ))

    def test_grant_requires_exact_resource_binding(self):
        self.seed()

        with self.assertRaises(sa.exc.IntegrityError):
            with self.transaction() as connection:
                connection.execute(sa.insert(grants).values(
                    tenant_id="tenant-a",
                    actor_id="actor-a",
                    resource_id="resource-a",
                    connector_id="connector-a",
                    provider_account_id="different-account",
                    action_type="create_case",
                    contract_version="v1",
                    enabled=True,
                ))

    def test_resource_cannot_change_account_by_duplication(self):
        self.seed()

        with self.assertRaises(sa.exc.IntegrityError):
            with self.transaction() as connection:
                connection.execute(sa.insert(resources).values(
                    tenant_id="tenant-a",
                    resource_id="resource-a",
                    connector_id="another-connector",
                    provider_account_id="another-account",
                    enabled=True,
                ))

    def test_invalid_identifiers_rejected(self):
        for tenant_id in ("", " ", "tenant-*", "tenant\nother"):
            with self.subTest(tenant_id=repr(tenant_id)):
                with self.assertRaises(sa.exc.IntegrityError):
                    with self.transaction() as connection:
                        connection.execute(sa.insert(actors).values(
                            tenant_id=tenant_id,
                            actor_id="actor-a",
                            enabled=True,
                        ))

    def test_revocation_updates_persisted_state(self):
        self.seed()

        with self.transaction() as connection:
            connection.execute(
                sa.update(grants)
                .values(enabled=False)
            )

        with self.transaction() as connection:
            enabled = connection.execute(
                sa.select(grants.c.enabled)
            ).scalar_one()

            self.assertIs(enabled, False)

    def test_admission_role_has_no_direct_table_access(self):
        with self.engine.connect() as connection:
            result = connection.exec_driver_sql(
                """
                SELECT
                    has_table_privilege(
                        'safeact_admission_writer',
                        quote_ident(%s) || '.safeact_auth_grants',
                        'SELECT'
                    ),
                    has_table_privilege(
                        'safeact_admission_writer',
                        quote_ident(%s) || '.safeact_auth_grants',
                        'UPDATE'
                    )
                """,
                (self.schema, self.schema),
            ).one()

            self.assertEqual(tuple(result), (False, False))


if __name__ == "__main__":
    unittest.main()
