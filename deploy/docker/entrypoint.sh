#!/bin/sh
# Write the local configuration storage/database.py expects (mode 600),
# drop the secrets from the environment, then start the component.
# Every service of the stack shares one network namespace, so PostgreSQL
# and Redis are on loopback, as the local-only guards require.
set -eu
: "${SAFEACT_DEV_DB_PASSWORD:?Missing database password}"
: "${SAFEACT_DEV_REDIS_PASSWORD:?Missing Redis password}"
umask 077
{
    echo "SAFEACT_DEV_DB_PORT=5432"
    echo "SAFEACT_DEV_DB_PASSWORD=${SAFEACT_DEV_DB_PASSWORD}"
    echo "SAFEACT_DEV_REDIS_PORT=6379"
    echo "SAFEACT_DEV_REDIS_PASSWORD=${SAFEACT_DEV_REDIS_PASSWORD}"
} > /app/.env
unset SAFEACT_DEV_DB_PASSWORD SAFEACT_DEV_REDIS_PASSWORD
# The prefork worker's metrics directory lives on the /tmp tmpfs.
if [ -n "${PROMETHEUS_MULTIPROC_DIR:-}" ]; then
    mkdir -p "${PROMETHEUS_MULTIPROC_DIR}"
fi
exec "$@"
