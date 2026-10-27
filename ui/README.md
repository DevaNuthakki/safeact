# SafeAct desktop walkthrough

The default route (`/demo`) presents five chapters: the problem, the $2,450.00
refund, the agent-created email, the connected human decision and stale order,
and recorded results with limits. The existing console is available through
**Inspect console**. This is a frontend presentation of the existing API and MCP
rules; no backend permission or lifecycle changes are required.

## Prepare the running local demo

With the existing Compose demo stack running (root README, "Run it locally"),
Python 3.13 and Node 24 (`.nvmrc`), execute from the repository root:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-db.lock -r requirements-api.lock \
    -r requirements-mcp.lock
.venv/bin/python ui/scripts/prepare_walkthrough.py --compose
cd ui && npm ci
SAFEACT_API_URL=http://127.0.0.1:8080 npm run dev
```

Open `http://127.0.0.1:5173/demo`. The introduction and recorded results are
public. Use `demo-north-reviewer` and its locally generated password for the
three case chapters; print only that line with
`docker compose --env-file .env -f deploy/compose.stack.yaml exec api cat /state/demo-credentials | grep '^demo-north-reviewer='`.
The console built into the Compose stack (:8080) excludes the case index
(`.dockerignore`), so its case chapters always show the preparation steps. No agent key is entered in the browser. Existing console
sign-ins remain available at `/login?console=1`.

The preparation tool reads existing credentials into memory and uses the normal
API and scoped MCP interface. It reuses matching cases, submits missing mock
cases, captures a refused agent self-attestation, and demonstrates a stale order
approval by approving an additional provider lookup. It prepares the email's
investigator proposal but leaves the email decision to the reviewer. It never
resets the database or contacts a paid provider or model.

`ui/public/demo-cases.json` is a generated, git-ignored index containing operation
and proposal IDs plus captured refusal output. It contains no credentials.
The UI reads current operation/proposal state from the API; refusals captured
during preparation are explicitly labelled as recorded demo output.

Proposals expire according to the existing policy. Preparation reuses an open
email proposal until expiry; after it expires, rerun preparation to select a
fresh proposal without resending the email. An existing captured stale refusal
is reused after its record is checked. If the email was already
approved, the walkthrough displays its recorded reviewer-attested outcome.
Rebuilding the demo database invalidates earlier case IDs; prepare again.

For a locally started stack rather than Compose, use:

```sh
.venv/bin/python ui/scripts/prepare_walkthrough.py \
  --api http://127.0.0.1:8000 --mcp http://127.0.0.1:8002/mcp
```

Run preparation **before** `npm run build` to include this local case index in a
static build. Built evidence documents under `public/evidence/` are copies of
the repository's published reports; their consistency is checked by Vitest.

## Verification

```sh
npm test
npm run lint
npm run build
npx playwright test
```

Browser tests own port 5173 while running; stop the development UI first. They
use the existing disposable test stack. Walkthrough browser fixtures test both
themes, keyboard access, text reflow and navigation without deciding real demo
proposals. Real API/MCP authority and effect counts are covered by the existing
`tests.e2e_api.test_walkthrough` integration test.

Preparation rerun checks (from the repository root):

```sh
.venv/bin/python -m unittest discover -s ui/scripts -p 'test_*.py'
```
