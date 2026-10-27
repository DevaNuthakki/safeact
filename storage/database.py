"""Local development database connection configuration.

Production configuration and role separation will be
introduced before a deployable service is enabled.
"""

from pathlib import Path

from sqlalchemy.engine import URL


ROOT = Path(__file__).resolve().parents[1]


REQUIRED_KEYS = frozenset({"SAFEACT_DEV_DB_PORT", "SAFEACT_DEV_DB_PASSWORD"})
# Present once the local broker is provisioned (Phase C, C8).
OPTIONAL_KEYS = frozenset({
    "SAFEACT_DEV_REDIS_PORT", "SAFEACT_DEV_REDIS_PASSWORD",
})


def _local_settings():
    path = ROOT / ".env"

    if not path.is_file():
        raise RuntimeError("Missing local database configuration")

    if path.stat().st_mode & 0o077:
        raise RuntimeError("Unsafe local credential permissions")

    settings = {}

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()

        if not line or line.startswith("#"):
            continue

        key, separator, value = line.partition("=")

        if not separator or key in settings:
            raise RuntimeError("Invalid local configuration")

        settings[key] = value

    if (
        not REQUIRED_KEYS <= set(settings)
        or set(settings) - REQUIRED_KEYS - OPTIONAL_KEYS
    ):
        raise RuntimeError("Unexpected local configuration keys")

    return settings


def _port(value, what):
    port = int(value)
    if not 1 <= port <= 65535:
        raise RuntimeError(f"Invalid {what} port")
    return port


def local_database_url() -> URL:
    settings = _local_settings()
    port = _port(settings["SAFEACT_DEV_DB_PORT"], "database")

    if not settings["SAFEACT_DEV_DB_PASSWORD"]:
        raise RuntimeError("Missing database password")

    return URL.create(
        "postgresql+psycopg",
        username="safeact_dev",
        password=settings["SAFEACT_DEV_DB_PASSWORD"],
        host="127.0.0.1",
        port=port,
        database="safeact_dev",
    )


def local_admin_settings() -> dict:
    """psycopg keyword settings for the local development administrator."""

    url = local_database_url()
    return {
        "host": url.host,
        "port": url.port,
        "user": url.username,
        "password": url.password,
        "dbname": url.database,
        "connect_timeout": 5,
    }


def local_redis_url(db=0) -> str:
    """Local, password-protected Redis broker (localhost only)."""

    settings = _local_settings()
    password = settings.get("SAFEACT_DEV_REDIS_PASSWORD")
    if not password or not password.isalnum():
        raise RuntimeError("Missing or unsafe local Redis password")
    port = _port(settings.get("SAFEACT_DEV_REDIS_PORT", "56379"), "Redis")
    if type(db) is not int or not 0 <= db <= 15:
        raise RuntimeError("Invalid Redis database index")
    return f"redis://:{password}@127.0.0.1:{port}/{db}"
