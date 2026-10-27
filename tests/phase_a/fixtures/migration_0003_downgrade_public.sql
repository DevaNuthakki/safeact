DROP FUNCTION IF EXISTS "public"."safeact_lifecycle_append"(uuid, integer, text, text, uuid, uuid, timestamp with time zone, text, jsonb, text)
-- safeact:statement --
DROP FUNCTION IF EXISTS "public"."safeact_lifecycle_lock"(uuid)
-- safeact:statement --
DROP FUNCTION IF EXISTS "public"."safeact_record_attempt_outcome"(uuid, text, text, text, jsonb)
-- safeact:statement --
DROP FUNCTION IF EXISTS "public"."safeact_recovery_snapshot"(uuid)
-- safeact:statement --
DROP FUNCTION IF EXISTS "public"."safeact_operations_in_state"(text, integer)
-- safeact:statement --
DROP FUNCTION IF EXISTS "public"."safeact_recovery_transition"(uuid, integer, text, text, text, jsonb)
-- safeact:statement --
DROP FUNCTION IF EXISTS "public"."safeact_relay_pending"(integer)
-- safeact:statement --
DROP FUNCTION IF EXISTS "public"."safeact_claimable_operations"(integer)
-- safeact:statement --
DROP FUNCTION IF EXISTS "public"."safeact_record_outbox_delivery"(uuid)
-- safeact:statement --
DROP FUNCTION IF EXISTS "public"."safeact_record_dispatch_outcome"(uuid, integer, uuid, text, text, jsonb)
-- safeact:statement --
DROP FUNCTION IF EXISTS "public"."safeact_begin_dispatch"(uuid, integer, uuid, integer)
-- safeact:statement --
DROP FUNCTION IF EXISTS "public"."safeact_claim"(uuid, integer)
-- safeact:statement --
DROP VIEW IF EXISTS "public"."safeact_operation_current_state"
-- safeact:statement --
DROP INDEX IF EXISTS "public".ix_safeact_operations_created_at
-- safeact:statement --
DROP TABLE IF EXISTS "public"."safeact_outbox_deliveries"
-- safeact:statement --
DROP TABLE IF EXISTS "public"."safeact_attempt_outcomes"
-- safeact:statement --
DROP TABLE IF EXISTS "public"."safeact_operation_transitions"
-- safeact:statement --
DROP TABLE IF EXISTS "public"."safeact_legal_transitions"
