# Compose full stack and fresh-clone check (M5.14, A12)

The whole demo runs in containers from a fresh clone with one command, and a smoke
test proves the main path end to end. DEMO ONLY: mock providers, synthetic accounts
and the `safeact_demo` schema. The production boundary stays deny-only.

## What runs

`deploy/compose.stack.yaml` (project `safeact-stack`, separate from the development
database in `deploy/compose.yaml`) starts these services:

| Service | Image | Role |
|---|---|---|
| postgres | `postgres:17.11-bookworm@sha256:858bb5e9…` | Database; owns the shared network namespace and the two published ports |
| redis | `redis:7.4.2-bookworm@sha256:fbdbaea4…` | Broker, bound to 127.0.0.1, password from `.env` |
| bootstrap | Python image | One-shot: `alembic upgrade head`, admission-role provisioning in one transaction, demo rebuild (credentials mode 600 on the `demo-state` volume) |
| providers, worker, relay, reconciler, advancer, api, mcp | Python image | The process stack, one component per container, each with its own database role and metrics exporter on 127.0.0.1 |
| console | `nginxinc/nginx-unprivileged:1.29.8-alpine@sha256:0c79d56a…` serving the console built in `node:24.22.1-bookworm-slim@sha256:55fb5f5a…` | Console and same-origin proxy for the API and SSE (buffering off); the MCP server's one door |
| prometheus | `prom/prometheus:v3.7.3@sha256:49214755…` | Scrapes the six exporters inside the namespace; not published |
| toxiproxy | pinned as in `deploy/compose.yaml` | `--profile chaos` only |
| smoke | Python image | `--profile check` only: the fresh-clone smoke test |

The Python image is built from `python:3.13.1-slim-bookworm@sha256:031ebf3c…`. It
installs only the reviewed locks, runs as uid 10001 and holds no secret. Full digests
are in the Dockerfiles and the compose file; Phase A checks that every image and base
is pinned by digest.

## Design decisions

- **One network namespace, like a pod.** Every service joins the postgres
  container's namespace.
  - **Why:** `storage/database.py` connects to 127.0.0.1 only, and the API and MCP
    server refuse to bind anywhere else. Sharing loopback keeps those guards exactly
    as they are, instead of loosening them for containers.
  - **What is exposed:** only nginx listens beyond loopback. Only its two ports are
    published, and only on the host's 127.0.0.1: the console (default 8080) and MCP
    (default 8002).
- **Same origin.** nginx serves the built console and proxies the API paths and
  `/events`, as the Vite dev server does. Browser navigations (`Accept: text/html`)
  get the app.
  - **Why:** the `__Host-` Secure cookies work on `http://localhost`.
  - **Host header:** nginx passes `Host` through. The API compares `Origin` with its
    own scheme and host, and MCP's DNS-rebinding check accepts 127.0.0.1 or
    localhost on any port.
- **Secrets.**
  - **At start:** `.env` (from `.env.example`, mode 600) supplies the database and
    Redis passwords. The entrypoint writes the mode-600 local configuration the code
    expects and then unsets them.
  - **Never in an image:** `.dockerignore` excludes `.env*` (except the example),
    `.demo-credentials*`, `.demo-stripe*`, `*.key`, `.git`, `docs` and `.venv`.
- **Bootstrap once.** Every SafeAct service waits for the one-shot bootstrap to
  succeed.
  - **What went wrong first:** the first smoke attempt used a plain
    `docker compose run smoke`. Compose re-ran the completed bootstrap as part of the
    dependency chain. It rebuilt `safeact_demo` (new roles and passwords) under the
    running services, and the API's next login failed with `permission denied for
    schema safeact_demo`.
  - **Fix:** the smoke test now runs with `--no-deps`. The compose file explains this,
    and says to recreate the services if the bootstrap is run again.
- **Each Python service builds the same Dockerfile.** BuildKit's cache makes that a
  single build. A shared image tag made the parallel builds collide.

## Fresh-clone check

`deploy/fresh_clone_check.sh [repository] [revision]` follows these steps:
1. It clones the committed revision into an empty temporary directory.
2. It writes `.env` with throwaway passwords and refuses unless the file is mode 600.
3. It builds the images and runs `up --wait`.
4. It runs the smoke test inside the stack.
5. It removes the stack, its volumes and the clone, on success or failure.

`.github/workflows/fresh-clone.yml` runs the same script on every push to main and on
manual dispatch, never on pull requests. It has not run on GitHub yet: nothing has
been pushed to main since it was added.

The smoke test (`deploy/docker/smoke.py`, through nginx as a browser would):
1. It signs in as `demo-north-requester` and takes the change feed's horizon.
2. It submits a refund.
3. It follows SSE with `Last-Event-ID` until the feed names the operation and REST
   says SUCCEEDED.
4. It loads a console page on an API path.
5. Inside the stack, it checks that all six metrics targets are `up` in Prometheus and
   that MCP lists its tools for the demo agent key.

| Run (2026-10-25, this laptop) | Commit | Build | Healthy after | Total | Result |
|---|---|---|---:|---:|---|
| warm: `fresh-clone-60c7f35-warm.log` | `60c7f35` | local build cache | 26 s | 33 s | PASS |
| cold: `fresh-clone-5ee1a8d-cold.log` | `5ee1a8d` | `SAFEACT_FRESH_NO_CACHE=1` (`--no-cache --pull`) | 96 s | 103 s | PASS |

The two run logs follow the logs rule and stay outside git, in
`~/safeact-evidence/docs/phase-e/compose-v1/`. Their SHA-256 digests are in
[artifact-sha256.json](artifact-sha256.json) and [../evidence-index.md](../evidence-index.md).

Both runs: refund SUCCEEDED and seen in the change feed; console page served; api,
mcp, worker, relay, reconciler and advancer targets `up`; MCP tools
`get_action_status`, `inspect_evidence`, `propose_resolution`,
`request_verification` and `submit_action`.

Environment: MacBookPro15,1, macOS 15.4.1, Docker Engine 29.2.1 (Docker Desktop,
linux/amd64), Compose 5.0.2.

## Checked by hand on the running stack (working tree before commit `60c7f35`)

- **Published ports:** `127.0.0.1:18080 → 8080` and `127.0.0.1:18002 → 8002`, and
  nothing else.
- **Browser sign-in:** a headless Chromium (Playwright) signed in at
  `http://localhost:18080`. It received `__Host-safeact_session` and
  `__Host-safeact_csrf`, both Secure, and the console showed the Actions view with the
  live indicator.
- **Origin check:** a login with `Origin: http://evil.example` got 403
  `cross_site_refused`. A same-origin bad login got 401.
- **No metrics through the door:** `/metrics` through the console returns the app's
  HTML, with no metric lines.
- **MCP door:** `POST /mcp` without a key got 401, and any other path on the MCP port
  got 404.
- **Image:** runs as `uid=10001(safeact)`. `/app/.env` is the empty mode-600
  placeholder until the entrypoint fills it, and the image holds no
  `.demo-credentials` or key file.

## Tests

`tests/phase_a/test_compose_stack.py` (6 tests) checks the rules statically, with no
Docker:
- every image and Dockerfile base is pinned by digest;
- only the two loopback doors are published;
- PostgreSQL and Redis listen on 127.0.0.1;
- every service shares the postgres namespace;
- services wait for the bootstrap, and the check runs the smoke test with `--no-deps`;
- `.dockerignore` and the entrypoint keep secrets out;
- the bootstrap refuses psql meta-commands and runs the provisioning file as one
  transaction.

## Limits

- **Demo only:** mock providers, synthetic accounts, one host and no TLS. The Stripe
  test-mode profile (M5.15) is not part of the Compose stack.
- **Credentials on rerun:** the bootstrap regenerates every demo credential each time
  it runs.
- **Not CI-proven yet:** the CI run of `fresh-clone.yml` is still pending (see above).

[artifact-sha256.json](artifact-sha256.json) seals the two logs (held outside git) and this review.
