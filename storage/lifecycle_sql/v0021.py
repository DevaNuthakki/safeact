"""FROZEN once committed: SQL for revision 0021_workflow_replay.

Bind a repeated workflow request to its immutable original charge as well as
the order/compensation plan. 0012 omitted fee, customer and charge contract
from its replay comparison. Existing rows require no backfill.
"""

from psycopg import sql

from storage.lifecycle_sql import v0003, v0012


REVISION = "0021_workflow_replay"
NEW_TABLES = ()
HISTORY_TABLES = ()
NEW_INDEXES = ()
LEGAL_TRANSITIONS = frozenset()
NEW_FUNCTIONS = {}
REPLACED_FUNCTIONS = ("safeact_api_create_workflow",)
SHARED_FUNCTIONS = {}
WORKER_FUNCTIONS = {}
ADVANCER_FUNCTIONS = {}
RECOVERY_FUNCTIONS = {}
INTERNAL_FUNCTIONS = {}
OWNER_TABLE_PRIVILEGES = {}


def _replace(statement):
    text = statement.as_string(None)
    if not text.startswith("CREATE FUNCTION "):
        raise RuntimeError("0012 workflow function header changed shape")
    return sql.SQL("CREATE OR REPLACE" + text[len("CREATE"):])


def _create_workflow(s):
    base = v0012._create_workflow(s).as_string(None)
    original = "OR v_existing.created_by IS DISTINCT FROM v.principal_id::text THEN"
    if base.count(original) != 1:
        raise RuntimeError("0012 workflow admission changed shape")
    bound = sql.SQL("""OR v_existing.created_by IS DISTINCT FROM v.principal_id::text
           OR NOT EXISTS (
               SELECT 1 FROM {s}.safeact_workflow_steps st
               JOIN {s}.safeact_operations o ON o.operation_id = st.operation_id
                WHERE st.workflow_id = v_existing.workflow_id
                  AND st.step = 'charge'
                  AND o.tenant_id = v.tenant_id
                  AND o.connector_id = 'mock-payments'
                  AND o.action_type = 'charge_shipping_fee'
                  AND o.resource_id = p_customer
                  AND o.provider_account_id = p_payments_account
                  AND o.contract_version = p_charge_version
                  AND o.payload_canonical = p_charge_payload
           ) THEN""").format(s=s).as_string(None)
    return _replace(sql.SQL(base.replace(original, bound)))


def upgrade_statements(schema):
    return [_create_workflow(v0003._schema(schema))]


def downgrade_statements(schema):
    return [_replace(v0012._create_workflow(v0003._schema(schema)))]
