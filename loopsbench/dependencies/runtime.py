"""Compose integration and host-side health/error accounting."""
from __future__ import annotations

import configparser
import io
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from loopsbench.dependencies.config import DependencyAccess, DependencyService
from loopsbench.dependencies.prepare import TOOLS, runtime_environment

SERVICE = "loopsbench-dependencies"
NETWORK = "loopsbench-dependency-access"
IMAGE = "loopsbench-dependencies:v2"
MOUNT = "/opt/loopsbench-dependencies"
CONFIG = "/opt/loopsbench-dependency-config.json"


class DependencyInfrastructureError(RuntimeError):
    pass


class DependencyRuntime:
    def __init__(self, access: DependencyAccess, service: DependencyService, directory: Path):
        self.access = access
        self.service = service
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.endpoint = service.endpoint or "http://loopsbench-dependencies:8080"
        self.manager = None
        self.evaluation_lock = threading.RLock()
        self.client_environment: dict[str, str] = {}
        self.client_phase_files: dict[str, Path] = {}
        self.client_preparation: dict[str, str] = {}
        self.phase_path = directory / "client-phase"
        self.phase_path.write_text("preparation")
        # Host updates in place; container access is read-only via the mount.
        self.phase_path.chmod(0o644)

    @property
    def resets_system_packages(self):
        return "apt" in self.access.ecosystems and bool(self.service.apt_repositories)

    def reset_tester(self, snapshot_path: str, workspace: str) -> bool:
        """apt/dpkg state needs a fresh writable layer, not a cache deletion.

        Direct evaluations already copied /workspace. Preserve only that tree;
        regression snapshots remain available in their existing trusted mount.
        The shared evaluation lock covers recreation, copying and execution.
        """
        if not self.resets_system_packages:
            return False
        from loopsbench.terminal.docker_compose_manager import (
            docker_command,
            docker_env,
        )

        try:
            with tempfile.TemporaryFile() as archive:
                if snapshot_path == workspace:
                    # Docker's archive API can silently omit tmpfs contents.
                    # Read/write within the container's mount namespace instead.
                    subprocess.run(docker_command(["exec", self.manager.tester_container.name,
                                                   "tar", "-C", workspace, "-cf", "-", "."]),
                                   stdout=archive, stderr=subprocess.PIPE, check=True,
                                   timeout=self.access.prepare_timeout_sec, env=docker_env())
                    archive.seek(0)
                self.manager._run_compose(["up", "-d", "--no-build", "--no-deps", "--force-recreate", "--renew-anon-volumes", "tester"])
                container = self.manager._service_container("tester")
                if container is None:
                    raise RuntimeError("fresh tester did not start")
                self.manager._tester_container = container
                if snapshot_path == workspace:
                    result = container.exec_run(["mkdir", "-p", workspace])
                    if result.exit_code:
                        raise RuntimeError("cannot create workspace in fresh tester")
                    subprocess.run(docker_command(["exec", "-i", container.name, "tar", "-C", workspace, "-xf", "-"]),
                                   stdin=archive, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                                   timeout=self.access.prepare_timeout_sec, env=docker_env())
            return True
        except Exception as exc:
            raise DependencyInfrastructureError("cannot recreate tester for system dependency isolation") from exc

    @staticmethod
    def _pip_config(items: dict) -> str:
        parser = configparser.RawConfigParser()
        environment = {}
        flattened = {}

        def flatten(values):
            for name, value in values.items():
                if isinstance(value, dict):
                    flatten(value)
                else:
                    flattened[name] = value

        # pip versions expose either a flat option mapping or grouped sections.
        flatten(items)
        for name, value in flattened.items():
            section, option = name.rsplit(".", 1)
            if section == ":env:":
                environment[option] = str(value)
                continue
            if not parser.has_section(section):
                parser.add_section(section)
            parser.set(section, option, str(value))
        # pip env options have priority over per-command config sections.
        for section in {"global", "install", "download", "wheel", "index", "list", *parser.sections()}:
            if not parser.has_section(section):
                parser.add_section(section)
            for key, value in environment.items():
                if key != "config-file" and value:
                    parser.set(section, key, value)
        stream = io.StringIO()
        parser.write(stream)
        return stream.getvalue()

    def initialize_client(self, container):
        """Capture package-manager settings before any agent/candidate code runs.

        Compose env is immutable after container creation. Its empty values
        deliberately defer to normal pip/Go config files, whose bind-mounted
        inodes the host updates at phase boundaries. No shell/agent API changes.
        """
        tester = self.manager.tester_container
        if tester is None:
            raise DependencyInfrastructureError("managed dependencies require a tester container")
        probes = ["command -v python3 >/dev/null", "command -v timeout >/dev/null"]
        if self.resets_system_packages:
            probes.append("command -v tar >/dev/null")
        if "go" in (self.access.prepare or []):
            probes.append("command -v go >/dev/null")
        if "python" in (self.access.prepare or []):
            probes.append(shlex.join([self.access.python.interpreter, "-I", "-c", "import venv, ensurepip"]))
        if tester.exec_run(["bash", "-c", " && ".join(probes)]).exit_code:
            raise DependencyInfrastructureError("task image lacks tools required for managed dependency preparation")
        image = self.manager._client.images.get(container.attrs["Image"])
        baseline = dict(item.split("=", 1) for item in image.attrs.get("Config", {}).get("Env", []) if "=" in item)
        baseline.update(self.client_environment)
        original_env = {key: baseline.get(key) or "" for key in self.client_managed_env}
        if "python" in self.access.ecosystems and container.exec_run(
                [self.access.python.interpreter, "-I", "-c", "import pip"], environment=original_env).exit_code == 0:
            script = "import json; from pip._internal.configuration import Configuration; c=Configuration(isolated=False); c.load(); print(json.dumps(dict(c.items())))"
            result = container.exec_run([self.access.python.interpreter, "-I", "-c", script], environment=original_env)
            if result.exit_code:
                raise DependencyInfrastructureError("cannot read original client pip configuration")
            self.client_preparation["python"] = self._pip_config(json.loads(result.output))
        if "go" in self.access.ecosystems and container.exec_run(["sh", "-c", "command -v go >/dev/null"]).exit_code == 0:
            keys = [key for key in self.client_managed_env if key.startswith("GO") and key != "GOENV"]
            result = container.exec_run(["go", "env", "-json", *keys], environment=original_env)
            if result.exit_code:
                raise DependencyInfrastructureError("cannot read original client Go configuration")
            values = json.loads(result.output)
            self.client_preparation["go"] = "".join(f"{key}={value}\n" for key, value in values.items())
        self.client_phase(False)

    def client_phase(self, solving: bool):
        self.phase_path.write_text("solving" if solving else "preparation")
        managed = runtime_environment({"access": self.access.model_dump(), "endpoint": self.endpoint}, "client", {})
        for ecosystem, path in self.client_phase_files.items():
            content = self.client_preparation.get(ecosystem, "")
            if solving and ecosystem == "python":
                values = {key.removeprefix("PIP_").lower().replace("_", "-"): value
                          for key, value in managed.items() if key.startswith("PIP_") and key != "PIP_CONFIG_FILE"}
                values.update({"extra-index-url": "", "find-links": ""})
                parser = configparser.RawConfigParser()
                for section in ("global", "install", "download", "wheel", "index", "list"):
                    parser[section] = values
                stream = io.StringIO()
                parser.write(stream)
                content = stream.getvalue()
            elif solving and ecosystem == "go":
                content = "".join(f"{key}={value}\n" for key, value in managed.items() if key.startswith("GO") and key != "GOENV")
            # Preserve the inode: Docker bind mounts must see each phase update.
            path.write_text(content)

    def compose(self, document: dict) -> dict:
        services = document["services"]
        if SERVICE in services:
            raise ValueError("reserved harness dependency service name")
        for name in ("client", "tester"):
            for mount in services.get(name, {}).get("volumes", []):
                source = mount.get("source", "") if isinstance(mount, dict) else mount.split(":")[0]
                if source and Path(source).is_absolute():
                    source = Path(source).resolve()
                    private = self.directory.resolve()
                    if private.is_relative_to(source) or source.is_relative_to(private):
                        raise ValueError("task mount exposes private dependency service state")
        code = Path(__file__).parent.resolve()
        # Stage only the runtime launcher with readable permissions, including
        # when the harness checkout was created with a restrictive umask.
        launcher = self.directory / "prepare.py"
        shutil.copyfile(code / "prepare.py", launcher)
        launcher.chmod(0o444)
        client_config = {"access": self.access.model_dump(), "endpoint": self.endpoint,
                         "wrapper_bin": MOUNT + "/bin", "phase_file": MOUNT + "/phase",
                         "apt_repositories": {name: repo.model_dump() for name, repo in self.service.apt_repositories.items()}}
        runtime_path = self.directory / "runtime.json"
        runtime_path.write_text(json.dumps(client_config))
        runtime_path.chmod(0o444)
        if self.service.provider == "docker":
            networks = document.setdefault("networks", {})
            if NETWORK in networks:
                raise ValueError("reserved harness dependency network name")
            networks[NETWORK] = {"internal": True}
            networks.setdefault("default", {})
            data = self.directory / "data"
            if self.service.replay_from:
                shutil.copytree(Path(self.service.replay_from).resolve(), data)
            else:
                data.mkdir(exist_ok=True)
            # Keep evidence private to the runner account. Do not make the
            # cache world-writable merely to accommodate the image's user.
            gateway_uid = os.getuid() or 65534
            gateway_gid = os.getgid() if os.getuid() else 65534
            for root, dirs, files in os.walk(data):
                os.chmod(root, 0o700)
                if os.getuid() == 0:
                    os.chown(root, gateway_uid, gateway_gid)
                for name in files:
                    os.chmod(Path(root) / name, 0o600)
                    if os.getuid() == 0:
                        os.chown(Path(root) / name, gateway_uid, gateway_gid)
            config_path = self.directory / "gateway.json"
            config_path.write_text(json.dumps({"policy": self.access.wire_policy(), "service": self.service.model_dump()}))
            config_path.chmod(0o444)
            services[SERVICE] = {
                "image": IMAGE, "command": ["/config.json", "/data"],
                "user": f"{gateway_uid}:{gateway_gid}",
                "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
                "pids_limit": 128, "mem_limit": "768m", "init": True,
                "volumes": [f"{config_path}:/config.json:ro", f"{data}:/data"],
                "networks": {"default": {}, NETWORK: {}},
                "healthcheck": {"test": ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/healthz',timeout=2)"],
                                "interval": "1s", "timeout": "5s", "retries": 30},
            }
            document.setdefault("networks", {}).setdefault("default", {})
        for name in ("client", "tester"):
            if name not in services:
                raise ValueError("managed dependencies require both client and tester services")
            service = services[name]
            reserved = {CONFIG, MOUNT + "/prepare.py", "/tmp/loopsbench-dependencies",
                        MOUNT + "/bin", MOUNT + "/phase", "/tmp/loopsbench-client-dependencies",
                        "/opt/loopsbench-client-pip.conf", "/opt/loopsbench-client-go.env"}
            for mount in service.get("volumes", []):
                target = mount.get("target") if isinstance(mount, dict) else mount.split(":")[1 if ":" in mount else 0]
                if target in reserved:
                    raise ValueError(f"task volume conflicts with reserved dependency path: {target}")
                if name == "tester" and self.resets_system_packages:
                    readonly = mount.get("read_only", False) if isinstance(mount, dict) else "ro" in mount.split(":")[2:]
                    if not readonly and not any(Path(target).is_relative_to(root) for root in ("/workspace", "/snapshots", "/shared", "/logs")):
                        raise ValueError("managed apt cannot reset tester system state kept in writable task volumes: " + target)
            service.setdefault("volumes", []).extend([f"{launcher}:{MOUNT}/prepare.py:ro", f"{runtime_path}:{CONFIG}:ro"])
            # Solver can use ordinary pip/go commands. Do not put its agent
            # controller into the tester's fresh Python environment.
            role = "client" if name == "client" else "tester"
            env = runtime_environment(client_config, role, {})
            env.pop("PATH", None)
            env.pop("VIRTUAL_ENV", None)
            if name == "client":
                env.pop("PYTHONNOUSERSITE", None)
            current = service.setdefault("environment", {})
            if isinstance(current, list):
                raise ValueError("expected resolved Compose environment mapping")
            if name == "client":
                self.client_environment = dict(current)
                # New tools use phase-aware CLI wrappers. Leave their original
                # environment intact while agents install/initialize themselves.
                env = {key: value for key, value in env.items() if key.startswith(("PIP_", "GO"))}
                self.client_managed_env = dict(env)
                current.update({key: "" for key in env})
                for ecosystem, variable, destination in (
                    ("python", "PIP_CONFIG_FILE", "/opt/loopsbench-client-pip.conf"),
                    ("go", "GOENV", "/opt/loopsbench-client-go.env"),
                ):
                    if ecosystem in self.access.ecosystems:
                        phase_directory = self.directory / "client-environment"
                        phase_directory.mkdir(mode=0o700, exist_ok=True)
                        path = phase_directory / (ecosystem + ".conf")
                        path.write_text("")
                        # May contain the image's pre-existing private mirror
                        # credentials. Client already owned those credentials.
                        path.chmod(0o644)
                        self.client_phase_files[ecosystem] = path
                        current[variable] = destination
                        service["volumes"].append(f"{path}:{destination}:ro")
            else:
                current.update(env)
            wrapper_tools = [tool for tool, ecosystem in TOOLS.items() if ecosystem in self.access.ecosystems]
            if wrapper_tools:
                wrapper_dir = self.directory / (name + "-bin")
                wrapper_dir.mkdir(mode=0o755, exist_ok=True)
                for tool in wrapper_tools:
                    path = wrapper_dir / tool
                    path.write_text("#!/usr/bin/env python3\nimport os, sys\nos.execvp('python3', ['python3', '-I', "
                                    + repr(MOUNT + "/prepare.py") + ", " + repr(CONFIG) + ", 'tool', "
                                    + repr(role) + ", " + repr(tool) + ", *sys.argv[1:]])\n")
                    path.chmod(0o555)
                service["volumes"].extend([f"{wrapper_dir}:{MOUNT}/bin:ro", f"{self.phase_path}:{MOUNT}/phase:ro"])
                if self.manager is None:
                    raise ValueError("dependency wrappers require resolved task image configuration")
                image = self.manager._client.images.get(service["image"])
                image_env = dict(item.split("=", 1) for item in image.attrs.get("Config", {}).get("Env", []) if "=" in item)
                current["PATH"] = MOUNT + "/bin:" + (current.get("PATH") or image_env.get("PATH") or os.defpath)
            if self.service.provider == "docker":
                networks = service.setdefault("networks", {"default": {}})
                if isinstance(networks, list):
                    networks = service["networks"] = {name: {} for name in networks}
                networks[NETWORK] = {}
                dependencies = service.setdefault("depends_on", {})
                if isinstance(dependencies, list):
                    dependencies = service["depends_on"] = {item: {"condition": "service_started"} for item in dependencies}
                dependencies[SERVICE] = {"condition": "service_healthy"}
        return document

    def status(self) -> dict:
        try:
            if self.service.provider == "docker":
                if self.manager is None:
                    raise RuntimeError("dependency runtime has no manager")
                container = self.manager._service_container(SERVICE)
                if container is None:
                    raise RuntimeError("dependency gateway container is missing")
                result = container.exec_run(["python3", "-c", "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8081/status',timeout=3).read().decode())"])
                if result.exit_code != 0:
                    raise RuntimeError("dependency gateway health request failed")
                payload = json.loads(result.output)
            else:
                opener = build_opener(ProxyHandler({}))
                with opener.open(self.endpoint.rstrip("/") + "/status", timeout=5) as response:
                    payload = json.load(response)
            if payload["protocol"] != 1 or payload["policy_digest"] != self.access.policy_digest():
                raise RuntimeError("dependency gateway policy does not match this task")
            if (not isinstance(payload.get("instance_id"), str) or not payload["instance_id"]
                    or any(not isinstance(payload["infrastructure_errors"][role], int)
                           or payload["infrastructure_errors"][role] < 0 for role in ("client", "tester"))):
                raise RuntimeError("invalid dependency gateway status protocol")
            return payload
        except Exception as exc:
            raise DependencyInfrastructureError("dependency gateway unavailable or policy mismatch") from exc

    def transport_errors(self) -> tuple[str, ...]:
        """Read trusted proxy evidence, never candidate package-manager output.

        A proxy may close a request before the gateway can record delivery
        failure. Its own log makes that an infrastructure error immediately.
        """
        if self.manager is None:
            return ()
        endpoint_host = urlsplit(self.endpoint).hostname
        errors = []
        try:
            for role, name in self.manager._network_controllers.items():
                container = self.manager._service_container(name)
                if container is None:
                    raise RuntimeError("dependency egress controller is missing")
                result = container.exec_run(["cat", "/tmp/gost.log"])
                if result.exit_code:
                    raise RuntimeError("cannot read dependency egress evidence")
                events = []
                sessions = set()
                for line in result.output.decode(errors="replace").splitlines():
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict) or not event.get("sid"):
                            continue
                        events.append(event)
                        if urlsplit("//" + event.get("host", "")).hostname == endpoint_host:
                            sessions.add(event["sid"])
                    except (ValueError, TypeError):
                        continue
                errors.extend({"role": role, **event} for event in events
                              if event.get("level") == "error" and event["sid"] in sessions)
        except Exception as exc:
            raise DependencyInfrastructureError("cannot inspect dependency gateway transport") from exc
        (self.directory / "transport-errors.json").write_text(json.dumps(errors, indent=2))
        return tuple(sorted(json.dumps(event, sort_keys=True) for event in errors))

    def checkpoint(self) -> tuple[str, int, int, tuple[str, ...]]:
        status = self.status()
        # A submitted lockfile can contain a URL copied from the client's
        # registry view. Route labels are not authenticated caller identities.
        # Conservatively invalidate on either counter while a test is running.
        return (status["instance_id"], status["infrastructure_errors"]["client"],
                status["infrastructure_errors"]["tester"], self.transport_errors())

    def record_service(self):
        status = self.status()
        record = {"access": self.access.model_dump(), "service": self.service.model_dump(),
                  "endpoint": self.endpoint, "status_at_start": status}
        if self.service.provider == "docker":
            record["image_id"] = self.manager._service_container(SERVICE).attrs["Image"]
        (self.directory / "service.json").write_text(json.dumps(record, indent=2))

    def check_since(self, before: tuple[str, int, int, tuple[str, ...]]):
        if self.checkpoint() != before:
            raise DependencyInfrastructureError("dependency upstream/cache/transport failed during snapshot evaluation; result is not test evidence")

    def prepare_argv(self, workspace: str) -> list[str]:
        return ["timeout", "--signal=TERM", "--kill-after=5s", str(self.access.prepare_timeout_sec),
                "python3", "-I", MOUNT + "/prepare.py", CONFIG, "prepare", workspace]

    def exec_argv(self) -> list[str]:
        return ["python3", "-I", MOUNT + "/prepare.py", CONFIG, "exec"]
