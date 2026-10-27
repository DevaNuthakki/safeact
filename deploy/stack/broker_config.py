"""Private broker credentials are accepted only for disposable qualification slots."""

import re


KEYS = frozenset({"SAFEACT_QUAL_REDIS_PORT", "SAFEACT_QUAL_REDIS_PASSWORD"})


def qualification_broker_url(values, redis_db, queue):
    present = KEYS & values.keys()
    if not present:
        return None
    if (present != KEYS or not re.fullmatch(r"safeact_it_[0-9a-f]{32}", values["SAFEACT_DEMO_SCHEMA"])
            or type(redis_db) is not int or redis_db not in range(2, 9)
            or not re.fullmatch(r"safeact-q[0-6]-[0-9a-f]{6}", queue)
            or int(queue[9]) + 2 != redis_db):
        raise RuntimeError("Private Redis credentials require a disposable qualification slot")
    port, password = values["SAFEACT_QUAL_REDIS_PORT"], values["SAFEACT_QUAL_REDIS_PASSWORD"]
    if (not re.fullmatch(r"[1-9][0-9]{3,4}", port) or not 1024 <= int(port) <= 65535
            or not re.fullmatch(r"[0-9a-f]{64}", password)):
        raise RuntimeError("Invalid private Redis credentials")
    return f"redis://:{password}@127.0.0.1:{port}/{redis_db}"
