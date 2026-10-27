"""Join an actual empty provider lookup to its completed HTTP exchange."""


def empty_lookup_windows(raw):
    from chaos.coverage import _pairs

    def timed(events, source):
        cutoff = raw.get("observation_finished_monotonic_ns", float("inf"))
        return [e for e in events if e.get("source") == source
                and type(e.get("monotonic_ns")) is int and 0 < e["monotonic_ns"] <= cutoff]

    provider = timed(raw.get("ledger", []), "provider")
    http = timed(raw.get("http", []), "http_client")
    ppairs = _pairs(provider, "provider_start", {"provider_end", "provider_error"}, "provider_call_id")
    hpairs = _pairs(http, "http_start", {"http_response", "http_error"}, "request_id")
    paths = {"payments": ("lookup", "/v1/lookup"), "crm": ("search", "/records")}

    def joins(p, q, h, answer):
        return (h.get("provider") == p.get("provider")
                and h.get("correlation_sha256") == p.get("correlation_sha256")
                and h.get("method") == "GET"
                and (p.get("method"), h.get("path")) == paths.get(p.get("provider"))
                and h["monotonic_ns"] <= p["monotonic_ns"] <= q["monotonic_ns"] <= answer["monotonic_ns"])

    result = []
    for p, q in ppairs:
        if q["event"] != "provider_end" or q.get("lookup_status") != "absent" or q.get("lookup_records") != []:
            continue
        matches = [(h, answer) for h, answer in hpairs if joins(p, q, h, answer)]
        if len(matches) != 1:
            continue
        h, answer = matches[0]
        if (answer["event"] != "http_response" or answer.get("status_code") != 200
                or sum(joins(a, b, h, answer) for a, b in ppairs) != 1):
            continue
        possible = [start for start in http if start.get("event") == "http_start"
                    and all(start.get(k) == h.get(k) for k in ("provider", "method", "path", "correlation_sha256"))
                    and start["monotonic_ns"] <= p["monotonic_ns"]
                    and not any(end.get("event") in ("http_response", "http_error")
                                and end.get("request_id") == start.get("request_id")
                                and end["monotonic_ns"] < p["monotonic_ns"] for end in http)]
        starts = [start for start in provider if start.get("event") == "provider_start"
                  and all(start.get(k) == p.get(k) for k in ("provider", "method", "correlation_sha256"))
                  and h["monotonic_ns"] <= start["monotonic_ns"] <= answer["monotonic_ns"]]
        if len(possible) != 1 or len(starts) != 1:
            continue
        result.append(dict(provider=p["provider"], correlation_sha256=p["correlation_sha256"],
            provider_call_id=p["provider_call_id"], request_id=h["request_id"],
            lookup_started_ns=p["monotonic_ns"], lookup_finished_ns=q["monotonic_ns"],
            http_finished_ns=answer["monotonic_ns"]))
    return result
