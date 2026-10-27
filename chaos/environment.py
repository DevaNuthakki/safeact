"""The machine a run happened on (blueprint §12 "hardware and workload
recorded"; M5 decision: record it, load average per scenario).

``describe()`` is captured at the start and end of a run; ``sample()`` at
the start and end of every scenario. Times are recorded, never judged:
the load-suspect rule (frozen in the manifest) reads them afterwards.
"""

import os
import platform
import shutil
import subprocess
import sys
import time
import re
from pathlib import Path
from importlib.metadata import PackageNotFoundError, version


def _run(*command):
    try:
        return subprocess.run(command, capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def sample():
    one, five, fifteen = os.getloadavg()
    return {"at": time.time(), "load_1": round(one, 2),
            "load_5": round(five, 2), "load_15": round(fifteen, 2),
            'available_memory_bytes': available_memory()}


def available_memory():
    try:
        if sys.platform == 'darwin':
            value = subprocess.run(['vm_stat'], capture_output=True, text=True, timeout=1).stdout
            size = re.search(r'page size of (\d+) bytes', value)
            pages = re.findall(r'^Pages (?:free|inactive|speculative):\s+(\d+)\.', value, re.M)
            return int(size[1])*sum(map(int, pages)) if size and len(pages) == 3 else None
        value = re.search(r'^MemAvailable:\s+(\d+) kB', Path('/proc/meminfo').read_text(), re.M)
        return int(value[1])*1024 if value else None
    except (OSError, subprocess.SubprocessError):
        return None


def service_versions():
    """Read local service versions; connection credentials never enter output."""
    result = {}
    try:
        import psycopg
        from storage.database import local_admin_settings
        with psycopg.connect(**dict(local_admin_settings(), connect_timeout=2)) as connection:
            result['postgresql'] = connection.info.server_version
    except Exception:
        result['postgresql'] = None
    try:
        import redis
        from storage.database import local_redis_url
        with redis.Redis.from_url(local_redis_url(), socket_connect_timeout=2, socket_timeout=2) as client:
            result['redis'] = client.info('server')['redis_version']
    except Exception:
        result['redis'] = None
    try:
        import httpx2
        from chaos.toxiproxy import API
        with httpx2.Client(base_url=API, trust_env=False, timeout=2) as client:
            response = client.get('/version')
            response.raise_for_status()
            result['toxiproxy'] = response.json()['version']
    except Exception:
        result['toxiproxy'] = None
    return result


POWER_SUPPLIES = Path('/sys/class/power_supply')


def power_source(supplies=POWER_SUPPLIES):
    """'ac', 'battery', 'no_battery' (a machine without one) or None (unknown)."""
    if sys.platform == 'darwin':
        text = _run('pmset', '-g', 'ps') or ''
        if "'AC Power'" in text:
            return 'ac'
        if "'Battery Power'" in text:
            return 'battery'
        return None
    try:
        entries = [entry for entry in supplies.iterdir() if (entry/'type').is_file()]
        kinds = {entry: (entry/'type').read_text().strip() for entry in entries}
        batteries = [entry for entry, kind in kinds.items() if kind == 'Battery']
        if any((entry/'status').is_file() and (entry/'status').read_text().strip() == 'Discharging'
               for entry in batteries):
            return 'battery'
        if not batteries:
            return 'no_battery'
        return 'ac'
    except FileNotFoundError:
        return 'no_battery'
    except OSError:
        return None


def hold_awake(pid):
    """Keep the machine awake for the life of `pid`: ``caffeinate -i -w pid``.

    Returns (record, process). The assertion must be visible to pmset before
    a run may start; caffeinate exits by itself when the watched pid exits.
    Elsewhere there is no idle-sleep assertion tool: recorded, not faked.
    """
    if sys.platform != 'darwin':
        return dict(applicable=False, reason='not macOS: no caffeinate on this platform'), None
    command = ['caffeinate', '-i', '-w', str(pid)]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    marker = f'pid {process.pid}(caffeinate)'
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and process.poll() is None:
        if marker in (_run('pmset', '-g', 'assertions') or ''):
            return dict(applicable=True, command=command, pid=process.pid, watched_pid=pid,
                        started_at=time.time(), assertion_observed=True), process
        time.sleep(0.1)
    process.kill()
    process.wait()
    raise RuntimeError('caffeinate did not assert PreventUserIdleSystemSleep')


def awake_status(process):
    """Whether the run's own caffeinate still holds its assertion."""
    if process is None:
        return dict(applicable=False)
    running = process.poll() is None
    return dict(applicable=True, pid=process.pid, running=running, at=time.time(),
                assertion_observed=running and f'pid {process.pid}(caffeinate)'
                in (_run('pmset', '-g', 'assertions') or ''))


def describe():
    info = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "cpu_count": os.cpu_count(),
        "load": sample(),
    }
    info['versions'] = service_versions()
    info['versions']['node'] = _run('node', '--version')
    for package in ('celery', 'SQLAlchemy', 'httpx2', 'psycopg'):
        try:
            info['versions'][package] = version(package)
        except PackageNotFoundError:
            info['versions'][package] = None
    info['git_commit'] = _run('git', 'rev-parse', 'HEAD')
    if sys.platform == "darwin":
        info["cpu_model"] = _run("sysctl", "-n", "machdep.cpu.brand_string")
        info["memory_bytes"] = _run("sysctl", "-n", "hw.memsize")
        info["model"] = _run("sysctl", "-n", "hw.model")
        info["power"] = _run("pmset", "-g", "ps")
        assertions = _run("pmset", "-g", "assertions") or ""
        info["caffeinate_active"] = "caffeinate" in assertions
    else:
        info["cpu_model"] = _run("sh", "-c",
                                 "grep -m1 'model name' /proc/cpuinfo")
        info["memory"] = _run("sh", "-c", "grep MemTotal /proc/meminfo")
    info["power_source"] = power_source()
    if shutil.which("docker"):
        info["docker"] = _run("docker", "version", "--format",
                              "{{.Server.Version}}")
    return info
