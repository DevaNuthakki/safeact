"""Proposed persistent authority schema.

This is a contract only, not an applied migration.
Only isolated integration tests currently create these tables.

Production deployment must use an Alembic migration and
separate restricted administrator/function-owner roles.
"""

import sqlalchemy as sa


authority_metadata = sa.MetaData()


def _identity_check(*columns: str) -> str:
    """Exact identifiers; no empty or wildcard scopes."""
    return " AND ".join(
        (
            f"length({column}) > 0"
            f" AND {column} = btrim({column})"
            f" AND strpos({column}, '*') = 0"
            f" AND {column} !~ '[[:cntrl:]]'"
        )
        for column in columns
    )


actors = sa.Table(
    "safeact_auth_actors",
    authority_metadata,
    sa.Column("tenant_id", sa.String(128), primary_key=True),
    sa.Column("actor_id", sa.String(128), primary_key=True),
    sa.Column(
        "enabled", sa.Boolean, nullable=False,
        server_default=sa.false(),
    ),
    sa.CheckConstraint(
        _identity_check("tenant_id", "actor_id"),
        name="ck_safeact_auth_actors_scope",
    ),
)


identities = sa.Table(
    "safeact_auth_identities",
    authority_metadata,
    sa.Column("issuer", sa.String(512), primary_key=True),
    sa.Column("subject", sa.String(255), primary_key=True),
    sa.Column("tenant_id", sa.String(128), nullable=False),
    sa.Column("actor_id", sa.String(128), nullable=False),
    sa.Column(
        "enabled", sa.Boolean, nullable=False,
        server_default=sa.false(),
    ),
    sa.ForeignKeyConstraint(
        ["tenant_id", "actor_id"],
        [
            "safeact_auth_actors.tenant_id",
            "safeact_auth_actors.actor_id",
        ],
        ondelete="RESTRICT",
        name="fk_safeact_auth_identities_actor",
    ),
    sa.UniqueConstraint(
        "tenant_id", "actor_id",
        name="uq_safeact_auth_identities_actor",
    ),
    sa.CheckConstraint(
        _identity_check(
            "issuer", "subject", "tenant_id", "actor_id",
        ),
        name="ck_safeact_auth_identities_scope",
    ),
)


resources = sa.Table(
    "safeact_auth_resources",
    authority_metadata,
    sa.Column("tenant_id", sa.String(128), primary_key=True),
    sa.Column("resource_id", sa.String(255), primary_key=True),
    sa.Column("connector_id", sa.String(128), primary_key=True),
    sa.Column(
        "provider_account_id", sa.String(255), primary_key=True,
    ),
    sa.Column(
        "enabled", sa.Boolean, nullable=False,
        server_default=sa.false(),
    ),
    sa.UniqueConstraint(
        "tenant_id", "resource_id",
        name="uq_safeact_auth_resources_owner",
    ),
    sa.CheckConstraint(
        _identity_check(
            "tenant_id", "resource_id",
            "connector_id", "provider_account_id",
        ),
        name="ck_safeact_auth_resources_scope",
    ),
)


grants = sa.Table(
    "safeact_auth_grants",
    authority_metadata,
    sa.Column("tenant_id", sa.String(128), primary_key=True),
    sa.Column("actor_id", sa.String(128), primary_key=True),
    sa.Column("resource_id", sa.String(255), primary_key=True),
    sa.Column("connector_id", sa.String(128), primary_key=True),
    sa.Column(
        "provider_account_id", sa.String(255), primary_key=True,
    ),
    sa.Column("action_type", sa.String(128), primary_key=True),
    sa.Column("contract_version", sa.String(64), primary_key=True),
    sa.Column(
        "enabled", sa.Boolean, nullable=False,
        server_default=sa.false(),
    ),
    sa.ForeignKeyConstraint(
        ["tenant_id", "actor_id"],
        [
            "safeact_auth_actors.tenant_id",
            "safeact_auth_actors.actor_id",
        ],
        ondelete="RESTRICT",
        name="fk_safeact_auth_grants_actor",
    ),
    sa.ForeignKeyConstraint(
        [
            "tenant_id", "resource_id",
            "connector_id", "provider_account_id",
        ],
        [
            "safeact_auth_resources.tenant_id",
            "safeact_auth_resources.resource_id",
            "safeact_auth_resources.connector_id",
            "safeact_auth_resources.provider_account_id",
        ],
        ondelete="RESTRICT",
        name="fk_safeact_auth_grants_resource",
    ),
    sa.CheckConstraint(
        _identity_check(
            "tenant_id", "actor_id", "resource_id",
            "connector_id", "provider_account_id",
            "action_type", "contract_version",
        ),
        name="ck_safeact_auth_grants_scope",
    ),
)
