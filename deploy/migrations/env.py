"""SafeAct Alembic environment."""

from alembic import context
from sqlalchemy import create_engine, pool

from storage.database import local_database_url
from storage.durable_lifecycle_sql import autogenerate_include_object
from storage.schema import metadata


target_metadata = metadata


def run_migrations_offline():
    raise RuntimeError(
        "Offline migrations are disabled for SafeAct."
    )


def run_migrations_online():
    # Tests may hand in a connection to a throwaway database.
    supplied = context.config.attributes.get("connection")

    if supplied is not None:
        context.configure(
            connection=supplied,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=autogenerate_include_object,
            transactional_ddl=True,
        )

        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_engine(
        local_database_url(),
        poolclass=pool.NullPool,
        connect_args={"connect_timeout": 5},
    )

    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                include_object=autogenerate_include_object,
                transactional_ddl=True,
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
