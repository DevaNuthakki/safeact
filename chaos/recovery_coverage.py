"""Deadline, key expiry and post-escalation commit coverage from direct evidence.

The provider owns its outage/cache observations. An independent query reads
committed database state and compares database times. Only host monotonic
intervals order these two sources; configured schedules and arm views do not.
Coverage is independent of the oracle's safety and usefulness verdicts.
"""

import math


UNRESOLVED = frozenset({"DISPATCHING", "UNKNOWN", "VERIFYING", "ESCALATED_UNRESOLVED"})


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _samples(raw):
    admitted = {a["local_id"] for a in raw.get("admissions", [])
                if a.get("local_id") and not a.get("changed")}
    return [e for e in raw.get("database", [])
            if e.get("event") == "db_recovery_sample" and e.get("source") == "database"
            and e.get("operation_id") in admitted and e.get("provider")
            and e.get("correlation_sha256") and type(e.get("transition_seq")) is int
            and e["transition_seq"] >= 0
            and type(e.get("sample_started_ns")) is int
            and type(e.get("sample_finished_ns")) is int
            and 0 < e["sample_started_ns"] <= e["sample_finished_ns"] == e.get("monotonic_ns")]


def _same(a, b):
    return all(a.get(k) == b.get(k) for k in ("provider", "correlation_sha256"))


def observed(raw):
    from chaos.coverage import _pairs

    ledger = [e for e in raw.get("ledger", []) if e.get("source") == "provider"
              and type(e.get("monotonic_ns")) is int and e["monotonic_ns"] > 0]
    http = [e for e in raw.get("http", []) if e.get("source") == "http_client"]
    ppairs = _pairs(ledger, "provider_start", {"provider_end", "provider_error"}, "provider_call_id")
    hpairs = _pairs(http, "http_start", {"http_response", "http_error"}, "request_id")
    samples = _samples(raw)
    found = set()

    for sample in raw.get('database', []):
        if (sample.get('source') != 'database' or sample.get('event') != 'db_breaker_sample'
                or sample.get('operation_id') not in {a.get('local_id') for a in raw.get('admissions', []) if not a.get('changed')}
                or sample.get('state') not in ('UNKNOWN', 'VERIFYING', 'RETRY_ELIGIBLE')
                or sample.get('breaker_open') is not True or not sample.get('provider_account_id')
                or type(sample.get('consecutive_failures')) is not int or type(sample.get('breaker_threshold')) is not int
                or not 1 <= sample['breaker_threshold'] <= sample['consecutive_failures']
                or not _number(sample.get('open_until'))
                or type(sample.get('sample_started_ns')) is not int or type(sample.get('sample_finished_ns')) is not int
                or not 0 < sample['sample_started_ns'] <= sample['sample_finished_ns'] == sample.get('monotonic_ns')):
            continue
        if any(_same(p, sample) and p.get('method') in ('refund', 'charge', 'create_record', 'send')
            and q['event'] == 'provider_error' and _same(h, sample) and h.get('method') == 'POST'
            and h['monotonic_ns'] <= p['monotonic_ns'] <= q['monotonic_ns'] <= response['monotonic_ns'] < sample['sample_started_ns']
            and (response['event'] == 'http_error' or response.get('status_code', 0) >= 400)
            for p, q in ppairs for h, response in hpairs):
            found.add('breaker_open_with_unresolved_work')

    # A recorded begin/end brackets two non-overlapping queries of the same
    # immutable DB deadline, live then passed. Both endpoints must actually
    # fail requests/lookups during this continuously active outage.
    outages = [e for e in ledger if e.get("event") == "provider_outage_changed"]
    for begin in outages:
        if begin.get("active") is not True or not begin.get("outage_id"):
            continue
        matching = [e for e in outages if e.get("provider") == begin.get("provider")
                    and e.get("outage_id") == begin["outage_id"]]
        if len(matching) != 2:
            continue
        ends = [e for e in matching if e.get("active") is False
                and e["monotonic_ns"] > begin["monotonic_ns"]]
        if len(ends) != 1:
            continue
        end = ends[0]
        # Another control change makes continuity ambiguous.
        if any(e not in matching and e.get("provider") == begin.get("provider")
               and begin["monotonic_ns"] <= e["monotonic_ns"] <= end["monotonic_ns"] for e in outages):
            continue
        inside = [s for s in samples if s["provider"] == begin.get("provider")
                  and _number(s.get("investigation_deadline"))
                  and begin["monotonic_ns"] < s["sample_started_ns"]
                  <= s["sample_finished_ns"] < end["monotonic_ns"]]
        for before in inside:
            for after in inside:
                if (before.get("deadline_passed") is not False or after.get("deadline_passed") is not True
                        or before["operation_id"] != after["operation_id"] or not _same(before, after)
                        or before["investigation_deadline"] != after["investigation_deadline"]
                        or before["sample_finished_ns"] >= after["sample_started_ns"]):
                    continue
                failed = [(p, q) for p, q in ppairs if _same(p, before)
                    and q["event"] == "provider_error" and q.get("error_type") in (
                        "ProviderTimeout", "LookupUnavailable", "CrmTimeout", "CrmSearchUnavailable", "EmailTimeout")
                    and begin["monotonic_ns"] < p["monotonic_ns"] <= q["monotonic_ns"] < end["monotonic_ns"]]
                if any(_same(h, before) and h["monotonic_ns"] <= p["monotonic_ns"] <= q["monotonic_ns"]
                       <= response["monotonic_ns"] and (response["event"] == "http_error"
                            or response.get("status_code", 0) >= 500)
                       for p, q in failed for h, response in hpairs):
                    found.add("provider_outage_spanned_deadline")

    # Only actual cache entries can expire. A declared window, loss of a
    # never-stored key or a late empty lookup cannot stand in for removal.
    for expired in ledger:
        if (expired.get("event") != "provider_key_expired" or expired.get("provider") != "payments"
                or expired.get("key_present_before") is not True or expired.get("key_present_after") is not False
                or not _number(expired.get("age_seconds")) or not _number(expired.get("retention_seconds"))
                or not 0 < expired["retention_seconds"] <= expired["age_seconds"]):
            continue
        stored = [e for e in ledger if e.get("event") == "provider_key_stored" and _same(e, expired)
                  and e.get("effect_id") == expired.get("effect_id") and e.get("effect_id")
                  and e.get("retention_seconds") == expired["retention_seconds"]
                  and e["monotonic_ns"] < expired["monotonic_ns"]]
        if len(stored) != 1:
            continue
        if not any(e.get("event") == "effect" and e.get("correlation_sha256") == expired.get("correlation_sha256")
                   and e.get("effect_id") == expired.get("effect_id")
                   and e["monotonic_ns"] <= stored[0]["monotonic_ns"] for e in ledger):
            continue
        # A real lost response and independent unresolved samples on both
        # sides of removal establish that the outcome was still unknown.
        lost = any(_same(h, expired) and h.get("method") == "POST" and response["event"] == "http_error"
                   and h["monotonic_ns"] < stored[0]["monotonic_ns"] < response["monotonic_ns"]
                   < expired["monotonic_ns"] for h, response in hpairs)
        mine = [s for s in samples if _same(s, expired) and s.get("state") in UNRESOLVED]
        if lost and any(a["operation_id"] == b["operation_id"]
                        and a["sample_finished_ns"] < expired["monotonic_ns"] < b["sample_started_ns"]
                        for a in mine for b in mine):
            found.add("retention_expired_before_outcome")

    # A committed escalation is visible before the FIRST effect for that
    # operation. Earlier hidden effects becoming searchable do not qualify.
    for sample in samples:
        if (sample.get("state") != "ESCALATED_UNRESOLVED"
                or not _number(sample.get("escalated_at"))):
            continue
        mine = [e for e in ledger if e.get("correlation_sha256") == sample["correlation_sha256"]]
        effects = [e for e in mine if e.get("event") == "effect"]
        calls = [e for e in mine if e.get("event") == "call"]
        if not effects or not calls:
            continue
        first = min(e["monotonic_ns"] for e in effects)
        if (sample["sample_finished_ns"] >= first
                or not all(e["monotonic_ns"] < sample["sample_started_ns"] for e in calls if e["monotonic_ns"] < first)):
            continue
        if any(_same(h, sample) and h.get("method") == "POST" and response["event"] == "http_error"
               and h["monotonic_ns"] <= p["monotonic_ns"] <= call["monotonic_ns"] <= q["monotonic_ns"]
               <= response["monotonic_ns"] < sample["sample_started_ns"]
               for p, q in ppairs for h, response in hpairs for call in calls
               if _same(p, sample) and p.get("method") in ("refund", "charge", "create_record", "send")
               and q["event"] == "provider_error"):
            found.add("late_commit_after_escalation")
    return found
