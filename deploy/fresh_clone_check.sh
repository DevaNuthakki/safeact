#!/usr/bin/env bash
# Fresh-clone check (blueprint A12), DEMO ONLY. The same script runs locally
# and in .github/workflows/fresh-clone.yml:
#
#   deploy/fresh_clone_check.sh [repository] [revision]
#
# 1. clone the repository (default: this one, at HEAD; committed files only)
#    into an empty directory;
# 2. write .env from .env.example with throwaway passwords, mode 600;
# 3. build the images and start the Compose stack, waiting for health;
# 4. run the smoke test inside the stack: sign in, submit a refund, see it
#    SUCCEEDED through REST and the change feed (SSE), every metrics target
#    up, MCP tools listed with a demo agent key;
# 5. remove the stack, its volumes and the clone, also on failure.
#
# SAFEACT_FRESH_NO_CACHE=1 builds without the local build cache and re-pulls
# the pinned bases (as cold as CI) without pruning the machine's cache.
#
# Its own Compose project name and host ports (SAFEACT_CONSOLE_PORT /
# SAFEACT_MCP_PORT, default 18080 / 18002), so a running demo is untouched.
set -euo pipefail

repository="${1:-$(git rev-parse --show-toplevel)}"
revision="${2:-$(git -C "${repository}" rev-parse HEAD)}"
project="safeact-fresh-$(openssl rand -hex 4)"
workdir="$(mktemp -d)"
clone="${workdir}/safeact"
export SAFEACT_CONSOLE_PORT="${SAFEACT_CONSOLE_PORT:-18080}"
export SAFEACT_MCP_PORT="${SAFEACT_MCP_PORT:-18002}"

compose() {
    docker compose --project-name "${project}" --env-file "${clone}/.env" \
        -f "${clone}/deploy/compose.stack.yaml" "$@"
}

cleanup() {
    status=$?
    if [ -f "${clone}/deploy/compose.stack.yaml" ] && [ -f "${clone}/.env" ]; then
        if [ "${status}" -ne 0 ]; then
            compose ps -a || true
            compose logs --no-color --tail 60 || true
        fi
        compose --profile check down -v --remove-orphans || true
    fi
    rm -rf "${workdir}"
    exit "${status}"
}
trap cleanup EXIT

git clone --quiet --no-local "${repository}" "${clone}"
git -C "${clone}" checkout --quiet --detach "${revision}"
echo "fresh clone of $(git -C "${clone}" rev-parse --short HEAD) in ${clone} (project ${project})"

umask 077
sed -e "s/^SAFEACT_DEV_DB_PASSWORD=.*/SAFEACT_DEV_DB_PASSWORD=$(openssl rand -hex 24)/" \
    -e "s/^SAFEACT_DEV_REDIS_PASSWORD=.*/SAFEACT_DEV_REDIS_PASSWORD=$(openssl rand -hex 24)/" \
    "${clone}/.env.example" > "${clone}/.env"
chmod 600 "${clone}/.env"
mode="$(stat -c %a "${clone}/.env" 2>/dev/null || stat -f %Lp "${clone}/.env")"
if [ "${mode}" != "600" ]; then
    echo "refusing: .env must be mode 600 (is ${mode})" >&2
    exit 1
fi

started=$(date +%s)
if [ "${SAFEACT_FRESH_NO_CACHE:-0}" = "1" ]; then
    compose build --no-cache --pull
else
    compose build
fi
compose up --detach --wait --wait-timeout 300
echo "stack healthy after $(( $(date +%s) - started )) s"
# --no-deps: the stack is already up. Starting the dependency chain again
# would re-run the one-shot bootstrap, which rebuilds safeact_demo (new role
# passwords and credentials) under the running services.
compose --profile check run --rm --no-deps smoke
echo "fresh-clone check PASS in $(( $(date +%s) - started )) s"
