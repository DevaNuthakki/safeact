# SafeAct Python image (DEMO ONLY). The API, MCP server, worker, relay,
# reconciler, advancer, mock providers, migrations, demo bootstrap and the
# fresh-clone smoke test all run from it (deploy/compose.stack.yaml).
#
# One base, pinned by digest. Dependencies come only from the reviewed
# locks, as in CI. The image runs as an unprivileged user and holds no
# secret: deploy/docker/entrypoint.sh writes the local configuration file
# from the container's environment when the container starts.
FROM python:3.13.1-slim-bookworm@sha256:031ebf3cde9f3719d2db385233bcb18df5162038e9cda20e64e08f49f4b47a2f

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements-db.lock requirements-broker.lock requirements-api.lock \
     requirements-mcp.lock requirements-obs.lock ./
COPY deploy/security/requirements-identity.txt deploy/security/
RUN python -m pip install \
        -r requirements-db.lock -r requirements-broker.lock \
        -r requirements-api.lock -r requirements-mcp.lock \
        -r requirements-obs.lock -r deploy/security/requirements-identity.txt

COPY alembic.ini ./
COPY api api
COPY connectors connectors
COPY core core
COPY deploy deploy
COPY mcp_server mcp_server
COPY observability observability
COPY storage storage

# storage/database.py reads /app/.env and refuses any group or other
# permission bit. The demo credentials live on the shared /state volume.
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin safeact \
    && install -o safeact -g safeact -m 600 /dev/null /app/.env \
    && install -d -o safeact -g safeact -m 700 /state \
    && chmod 755 deploy/docker/entrypoint.sh

USER safeact
ENTRYPOINT ["/app/deploy/docker/entrypoint.sh"]
