"""Own one disposable Redis container. No action can target the shared broker.

Only its fixed loopback port is published. The authenticated Redis process is
PID 1, uses a tmpfs data directory, and has both RDB and AOF disabled. Docker
kill/die events, wait status, inspect state and Redis run ids prove the restart.
Container commands and credentials never enter the public evidence.
"""

import json
import os
import re
import secrets
import socket
import subprocess
import time

from datetime import datetime, timedelta


IMAGE = "redis:7.4.2-bookworm"
LABEL = "org.safeact.qualification.redis-owner"


class IsolatedRedis:
    def __init__(self, index):
        if type(index) is not int or index not in range(7):
            raise ValueError("Invalid isolated Redis slot")
        self.index, self.redis_db = index, index + 2
        self.owner = secrets.token_hex(16)
        self.name = "safeact-qual-redis-" + self.owner
        self._password = secrets.token_hex(32)
        self._id = self._image = None
        self.port = None
        self._attempted = self._removed = False
        self._deadline = None
        self.events = []

    def _docker(self, *args, timeout=15):
        if self._deadline is not None:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Isolated Redis fault deadline elapsed")
            timeout = min(timeout, remaining)
        try:
            result = subprocess.run(["docker", *args], capture_output=True,
                text=True, timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError("Isolated Redis Docker command could not finish") from None
        if result.returncode:
            raise RuntimeError("Isolated Redis Docker command failed")
        return result.stdout.strip()

    def _record(self, event, emit=None, **details):
        facts = dict(source="isolated_broker", container_ref=self._id[:12],
            owner=self.owner, redis_db=self.redis_db, port=self.port, **details)
        self.events.append(dict(facts, event=event, at=time.time(), monotonic_ns=time.monotonic_ns()))
        if emit is not None:
            emit(event, **facts)

    def credentials(self):
        if not self.port or not self._id or self._removed:
            raise RuntimeError("Private Redis is not available")
        return {"SAFEACT_QUAL_REDIS_PORT": str(self.port),
                "SAFEACT_QUAL_REDIS_PASSWORD": self._password}

    @property
    def url(self):
        self.credentials()
        return f"redis://:{self._password}@127.0.0.1:{self.port}/{self.redis_db}"

    def _command(self):
        return ["--bind", "0.0.0.0", "--port", "6379", "--protected-mode", "yes",
                "--requirepass", self._password, "--save", "", "--appendonly", "no",
                "--daemonize", "no", "--dir", "/data", "--logfile", ""]

    def _inspect(self):
        data = json.loads(self._docker("container", "inspect", self._id))
        if not isinstance(data, list) or len(data) != 1:
            raise RuntimeError("Invalid isolated Redis container inventory")
        row = data[0]
        if (row.get("Id") != self._id or row.get("Name") != "/" + self.name
                or row.get("Config", {}).get("Labels", {}).get(LABEL) != self.owner):
            raise RuntimeError("Isolated Redis ownership was not verified")
        return row

    def _validate(self, row, *, running):
        config, host, state = row["Config"], row["HostConfig"], row["State"]
        ports = {"6379/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(self.port)}]}
        mounts = host.get("Mounts", [])
        valid_mount = (len(mounts) == 1 and mounts[0].get("Type") == "tmpfs"
            and mounts[0].get("Target") == "/data" and not mounts[0].get("Source"))
        checks = {
            "image": row.get("Image") == self._image,
            "entrypoint": config.get("Entrypoint") == ["redis-server"] and row.get("Path") == "redis-server",
            "command": config.get("Cmd") == self._command(),
            "user": config.get("User") == "65534:65534",
            "tmpfs": valid_mount and all(m.get("Type") == "tmpfs" and m.get("Destination") == "/data"
                                         for m in row.get("Mounts", [])),
            "no_host_mounts": not host.get("Binds") and not host.get("VolumesFrom"),
            "privileges": not host.get("Privileged") and not host.get("PidMode"),
            "network": host.get("NetworkMode") in ("bridge", "default"),
            "loopback_port": host.get("PortBindings") == ports,
            "readonly_root": host.get("ReadonlyRootfs") is True,
            "restart_policy": host.get("RestartPolicy", {}).get("Name") == "no",
            "state": state.get("Running") is running and not any(state.get(k) for k in
                      ("Restarting", "Dead", "OOMKilled", "Error")),
        }
        if running:
            checks["live_endpoint"] = (row.get("NetworkSettings", {}).get("Ports") == ports
                and type(state.get("Pid")) is int and state["Pid"] > 1)
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            raise RuntimeError("Isolated Redis verification failed: " + ", ".join(failed))

    def start(self):
        if self._attempted:
            raise RuntimeError("Isolated Redis has already been created")
        endpoint = os.environ.get("DOCKER_HOST", "unix://local")
        contexts = json.loads(self._docker("context", "inspect"))
        if not isinstance(contexts, list) or len(contexts) != 1:
            raise RuntimeError("Docker context inventory was not verified")
        context = contexts[0].get("Endpoints", {}).get("docker", {}).get("Host", "")
        if not endpoint.startswith("unix://") or not isinstance(context, str) or not context.startswith("unix://"):
            raise RuntimeError("Isolated broker requires a local Docker daemon")
        self._image = self._docker("image", "inspect", IMAGE, "--format", "{{.Id}}")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self._image):
            raise RuntimeError("The existing Redis image could not be pinned by content id")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]
            self._attempted = True
            self._id = self._docker("container", "create", "--name", self.name,
                "--label", LABEL + "=" + self.owner, "--restart", "no", "--user", "65534:65534",
                "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
                "--pids-limit", "32", "--memory", "128m", "--publish", f"127.0.0.1:{self.port}:6379",
                "--mount", "type=tmpfs,destination=/data,tmpfs-size=33554432,tmpfs-mode=1777",
                "--entrypoint", "redis-server", self._image, *self._command())
        if not re.fullmatch(r"[0-9a-f]{64}", self._id):
            self._id = None
            raise RuntimeError("Isolated Redis create did not return a full container id")
        self._validate(self._inspect(), running=False)
        self._docker("container", "start", self._id)
        self._validate(self._inspect(), running=True)
        import redis
        client = redis.Redis.from_url(self.url, socket_connect_timeout=1, socket_timeout=1)
        try:
            self._ready(client)
            info = self._server(client)
            if client.dbsize() != 0:
                raise RuntimeError("A new isolated broker was not empty")
            self._record("isolated_redis_started", image=IMAGE, image_id_prefix=self._image[7:19], **info)
        finally:
            client.close()
        return self

    def _ready(self, client):
        from redis.exceptions import ConnectionError, TimeoutError
        deadline = time.monotonic() + 8
        while True:
            try:
                if client.ping() is True:
                    return
            except (ConnectionError, TimeoutError):
                pass
            limit = min(deadline, self._deadline) if self._deadline is not None else deadline
            if time.monotonic() >= limit:
                raise RuntimeError("Isolated Redis did not become ready") from None
            time.sleep(0.05)

    def _server(self, client):
        info = client.info("server")
        if (info.get("redis_version") != "7.4.2" or info.get("process_id") != 1
                or not re.fullmatch(r"[0-9a-f]{40}", info.get("run_id", ""))
                or client.config_get("save") != {"save": ""}
                or client.config_get("appendonly") != {"appendonly": "no"}):
            raise RuntimeError("Isolated Redis version, PID or persistence settings were not verified")
        return dict(run_id=info["run_id"], redis_version=info["redis_version"],
                    server_pid=1, rdb_enabled=False, aof_enabled=False)

    def crash_recover(self, client, queue, before, messages, emit, *, protocol='safeact'):
        self._deadline = time.monotonic() + 25
        try:
            return self._crash_recover(client, queue, before, messages, emit,protocol=protocol)
        finally:
            self._deadline = None  # Cleanup must still work after a timed-out fault.

    def _crash_recover(self, client, queue, before, messages, emit, *, protocol='safeact'):
        from chaos.broker_protocol import protocol as select_protocol
        wire=select_protocol(protocol)
        settings = client.connection_pool.connection_kwargs
        if (settings.get("host") != "127.0.0.1" or settings.get("port") != self.port
                or settings.get("db") != self.redis_db or settings.get("password") != self._password
                or not wire.valid_queue(queue)
                or (protocol=='safeact' and not re.fullmatch(rf"safeact-q{self.index}-[0-9a-f]{{6}}",queue))):
            raise RuntimeError("Refusing to kill a broker other than this newly owned container")
        row = self._inspect()
        self._validate(row, running=True)
        server = self._server(client)
        started = time.monotonic_ns()
        captured = client.lrange(queue, 0, -1)
        finished = time.monotonic_ns()
        if captured != before or not captured or len(captured) != len(messages):
            raise RuntimeError("Queued inventory changed before the isolated broker kill")
        self._record("isolated_redis_queued", emit, queue=queue, messages=messages,
            before_count=len(captured), sample_started_ns=started, sample_finished_ns=finished, **server)
        self._record("isolated_redis_kill_requested", emit, signal=9, run_id=server["run_id"],
                     daemon_pid=row["State"]["Pid"])
        self._docker("container", "kill", "--signal", "KILL", self._id)
        waited = self._docker("container", "wait", self._id, timeout=5)
        dead = self._inspect()
        self._validate(dead, running=False)
        if waited != "137" or dead["State"].get("ExitCode") != 137 or dead["State"].get("Pid") != 0:
            raise RuntimeError("Isolated Redis SIGKILL exit was not verified")
        until = (datetime.fromisoformat(dead["State"]["FinishedAt"].replace("Z", "+00:00"))
                 + timedelta(seconds=1)).isoformat()
        events = [json.loads(line) for line in self._docker("events", "--format", "{{json .}}",
            "--since", row["Created"], "--until", until, "--filter", "container=" + self._id,
            "--filter", "event=kill", "--filter", "event=die", timeout=5).splitlines()]
        mine = [e for e in events if e.get("Type") == "container" and e.get("Actor", {}).get("ID") == self._id]
        kills, deaths = ([e for e in mine if e.get("Action") == name] for name in ("kill", "die"))
        if (len(kills) != 1 or len(deaths) != 1 or kills[0]["Actor"]["Attributes"].get("signal") != "9"
                or deaths[0]["Actor"]["Attributes"].get("exitCode") != "137"
                or type(kills[0].get("timeNano")) is not int or type(deaths[0].get("timeNano")) is not int
                or kills[0]["timeNano"] <= 0 or deaths[0]["timeNano"] <= 0):
            raise RuntimeError("Docker daemon did not independently report the requested kill and death")
        self._record("isolated_redis_killed", emit, signal=9, run_id=server["run_id"],
            wait_exit_code=137, inspect_exit_code=137, oom_killed=False,
            daemon_kill_ns=kills[0]["timeNano"], daemon_die_ns=deaths[0]["timeNano"])
        self._docker("container", "start", self._id)
        self._validate(self._inspect(), running=True)
        client.connection_pool.disconnect()
        self._ready(client)
        restarted = self._server(client)
        started = time.monotonic_ns()
        with client.pipeline() as pipe:
            pipe.lrange(queue, 0, -1)
            pipe.dbsize()
            after, size = pipe.execute()
        finished = time.monotonic_ns()
        if restarted["run_id"] == server["run_id"] or after or size != 0:
            raise RuntimeError("Restarted Redis did not have a new run id and empty broker database")
        self._record("isolated_redis_restarted", emit, queue=queue, previous_run_id=server["run_id"],
            after_count=len(after), database_size_after=size,
            sample_started_ns=started, sample_finished_ns=finished, **restarted)

    def stop(self):
        if not self._attempted or self._removed:
            return
        if self._id is None:
            found = self._docker("container", "ls", "--all", "--quiet", "--no-trunc",
                "--filter", "name=^/" + self.name + "$", "--filter", "label=" + LABEL + "=" + self.owner)
            if not found:
                return
            if not re.fullmatch(r"[0-9a-f]{64}", found):
                raise RuntimeError("Ambiguous isolated Redis cleanup inventory")
            self._id = found
        self._inspect()  # Ownership is mandatory even during failed setup.
        self._docker("container", "rm", "--force", self._id)
        remaining = self._docker("container", "ls", "--all", "--quiet", "--no-trunc", "--filter", "id=" + self._id)
        if remaining:
            raise RuntimeError("Owned Redis container removal was not verified")
        self._removed = True
        self._record("isolated_redis_removed")
