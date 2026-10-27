GRANT USAGE ON SCHEMA "safeact_it_00000000000000000000000000000000" TO "safeact_fn_golden", "safeact_wk_golden_a", "safeact_wk_golden_b", "safeact_rc_golden"
-- safeact:statement --
GRANT SELECT ON "safeact_it_00000000000000000000000000000000"."safeact_operations" TO "safeact_fn_golden"
-- safeact:statement --
GRANT SELECT, INSERT ON "safeact_it_00000000000000000000000000000000"."safeact_attempts" TO "safeact_fn_golden"
-- safeact:statement --
GRANT INSERT ON "safeact_it_00000000000000000000000000000000"."safeact_audit_events" TO "safeact_fn_golden"
-- safeact:statement --
GRANT SELECT ON "safeact_it_00000000000000000000000000000000"."safeact_outbox_events" TO "safeact_fn_golden"
-- safeact:statement --
GRANT SELECT ON "safeact_it_00000000000000000000000000000000"."safeact_legal_transitions" TO "safeact_fn_golden"
-- safeact:statement --
GRANT SELECT, INSERT ON "safeact_it_00000000000000000000000000000000"."safeact_operation_transitions" TO "safeact_fn_golden"
-- safeact:statement --
GRANT SELECT, INSERT ON "safeact_it_00000000000000000000000000000000"."safeact_attempt_outcomes" TO "safeact_fn_golden"
-- safeact:statement --
GRANT INSERT ON "safeact_it_00000000000000000000000000000000"."safeact_outbox_deliveries" TO "safeact_fn_golden"
-- safeact:statement --
GRANT UPDATE ("operation_id") ON "safeact_it_00000000000000000000000000000000"."safeact_operations" TO "safeact_fn_golden"
-- safeact:statement --
ALTER FUNCTION "safeact_it_00000000000000000000000000000000"."safeact_claim"(uuid, integer) OWNER TO "safeact_fn_golden"
-- safeact:statement --
ALTER FUNCTION "safeact_it_00000000000000000000000000000000"."safeact_begin_dispatch"(uuid, integer, uuid, integer) OWNER TO "safeact_fn_golden"
-- safeact:statement --
ALTER FUNCTION "safeact_it_00000000000000000000000000000000"."safeact_record_dispatch_outcome"(uuid, integer, uuid, text, text, jsonb) OWNER TO "safeact_fn_golden"
-- safeact:statement --
ALTER FUNCTION "safeact_it_00000000000000000000000000000000"."safeact_record_outbox_delivery"(uuid) OWNER TO "safeact_fn_golden"
-- safeact:statement --
ALTER FUNCTION "safeact_it_00000000000000000000000000000000"."safeact_claimable_operations"(integer) OWNER TO "safeact_fn_golden"
-- safeact:statement --
ALTER FUNCTION "safeact_it_00000000000000000000000000000000"."safeact_relay_pending"(integer) OWNER TO "safeact_fn_golden"
-- safeact:statement --
ALTER FUNCTION "safeact_it_00000000000000000000000000000000"."safeact_recovery_transition"(uuid, integer, text, text, text, jsonb) OWNER TO "safeact_fn_golden"
-- safeact:statement --
ALTER FUNCTION "safeact_it_00000000000000000000000000000000"."safeact_operations_in_state"(text, integer) OWNER TO "safeact_fn_golden"
-- safeact:statement --
ALTER FUNCTION "safeact_it_00000000000000000000000000000000"."safeact_recovery_snapshot"(uuid) OWNER TO "safeact_fn_golden"
-- safeact:statement --
ALTER FUNCTION "safeact_it_00000000000000000000000000000000"."safeact_record_attempt_outcome"(uuid, text, text, text, jsonb) OWNER TO "safeact_fn_golden"
-- safeact:statement --
ALTER FUNCTION "safeact_it_00000000000000000000000000000000"."safeact_lifecycle_lock"(uuid) OWNER TO "safeact_fn_golden"
-- safeact:statement --
ALTER FUNCTION "safeact_it_00000000000000000000000000000000"."safeact_lifecycle_append"(uuid, integer, text, text, uuid, uuid, timestamp with time zone, text, jsonb, text) OWNER TO "safeact_fn_golden"
-- safeact:statement --
ALTER VIEW "safeact_it_00000000000000000000000000000000"."safeact_operation_current_state" OWNER TO "safeact_fn_golden"
-- safeact:statement --
REVOKE ALL ON "safeact_it_00000000000000000000000000000000"."safeact_operation_current_state" FROM "safeact_fn_golden"
-- safeact:statement --
GRANT SELECT ON "safeact_it_00000000000000000000000000000000"."safeact_operation_current_state" TO "safeact_fn_golden"
-- safeact:statement --
GRANT EXECUTE ON FUNCTION "safeact_it_00000000000000000000000000000000"."safeact_claim"(uuid, integer) TO "safeact_wk_golden_a", "safeact_wk_golden_b"
-- safeact:statement --
GRANT EXECUTE ON FUNCTION "safeact_it_00000000000000000000000000000000"."safeact_begin_dispatch"(uuid, integer, uuid, integer) TO "safeact_wk_golden_a", "safeact_wk_golden_b"
-- safeact:statement --
GRANT EXECUTE ON FUNCTION "safeact_it_00000000000000000000000000000000"."safeact_record_dispatch_outcome"(uuid, integer, uuid, text, text, jsonb) TO "safeact_wk_golden_a", "safeact_wk_golden_b"
-- safeact:statement --
GRANT EXECUTE ON FUNCTION "safeact_it_00000000000000000000000000000000"."safeact_record_outbox_delivery"(uuid) TO "safeact_wk_golden_a", "safeact_wk_golden_b"
-- safeact:statement --
GRANT EXECUTE ON FUNCTION "safeact_it_00000000000000000000000000000000"."safeact_claimable_operations"(integer) TO "safeact_wk_golden_a", "safeact_wk_golden_b"
-- safeact:statement --
GRANT EXECUTE ON FUNCTION "safeact_it_00000000000000000000000000000000"."safeact_relay_pending"(integer) TO "safeact_wk_golden_a", "safeact_wk_golden_b"
-- safeact:statement --
GRANT EXECUTE ON FUNCTION "safeact_it_00000000000000000000000000000000"."safeact_recovery_transition"(uuid, integer, text, text, text, jsonb) TO "safeact_rc_golden"
-- safeact:statement --
GRANT EXECUTE ON FUNCTION "safeact_it_00000000000000000000000000000000"."safeact_operations_in_state"(text, integer) TO "safeact_rc_golden"
-- safeact:statement --
GRANT EXECUTE ON FUNCTION "safeact_it_00000000000000000000000000000000"."safeact_recovery_snapshot"(uuid) TO "safeact_rc_golden"
-- safeact:statement --
GRANT EXECUTE ON FUNCTION "safeact_it_00000000000000000000000000000000"."safeact_record_attempt_outcome"(uuid, text, text, text, jsonb) TO "safeact_wk_golden_a", "safeact_wk_golden_b", "safeact_rc_golden"
-- safeact:statement --
GRANT SELECT ON "safeact_it_00000000000000000000000000000000"."safeact_operation_current_state" TO "safeact_wk_golden_a", "safeact_wk_golden_b", "safeact_rc_golden"
