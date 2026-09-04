"""Docker Compose lifecycle manager for LoopsBench containers."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generator, Mapping, Sequence

import yaml

from loopsbench.task_images.strategy import DockerImageStrategy
from loopsbench.utils.compose_env import compose_runtime_env
from loopsbench.utils.compose_security import (
    format_compose_security_issues,
    validate_compose_security,
)
from loopsbench.utils.env_model import EnvModel
from loopsbench.utils.logger import logger

if TYPE_CHECKING:
    from docker.models.containers import Container
else:
    Container = Any


_docker_module: Any | None = None


def _get_docker_module() -> Any:
    global _docker_module
    if _docker_module is None:
        import docker as docker_module

        _docker_module = docker_module
    return _docker_module


# ---------------------------------------------------------------------------
# Environment variable model (passed to docker compose)
# ---------------------------------------------------------------------------


@dataclass
class DockerComposeEnvVars(EnvModel):
    task_docker_client_container_name: str | None = None
    task_docker_client_image_name: str | None = None
    task_docker_tester_container_name: str | None = None
    task_docker_tester_image_name: str | None = None
    task_docker_name_prefix: str | None = None
    container_logs_path: str | None = None
    container_agent_logs_path: str | None = None
    test_dir: str | None = None
    task_logs_path: str | None = None
    task_agent_logs_path: str | None = None
    # Host path to the task directory; used by regression.compose.yaml bind mounts.
    task_dir: str | None = None


@dataclass(frozen=True)
class DockerImageResolution:
    requested_client_image_ref: str
    resolved_image_id: str | None = None
    resolved_repo_digest: str | None = None


@dataclass(frozen=True)
class EffectiveContainerEvidence:
    """Private runner evidence for one effective container generation."""

    container_id: str
    started_at: str
    image_id: str


class ContainerAdmissionError(RuntimeError):
    """Raised when effective-container admission cannot be proven."""


class _ComposeInterpolationError(ValueError):
    """Raised when static Compose interpolation cannot be parsed safely."""


_CREDENTIAL_ENV_NAME = re.compile(
    r"(?:^|_)(?:API_KEY|AUTH_TOKEN|ACCESS_TOKEN|TOKEN|ACCESS_KEY_ID|SECRET|"
    r"PASSWORD|PASSWD|CREDENTIALS?|PRIVATE_KEY|SESSION_TOKEN)(?:$|_)",
    re.IGNORECASE,
)
_CREDENTIAL_ENV_NAMES = {
    "AWS_CONFIG_FILE",
    "AWS_PROFILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AZURE_CONFIG_DIR",
    "DATABASE_DSN",
    "DATABASE_URI",
    "DATABASE_URL",
    "DB_DSN",
    "DB_URI",
    "DB_URL",
    "GIT_ASKPASS",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "JDBC_DATABASE_URL",
    "KUBECONFIG",
    "MONGODB_URI",
    "MONGODB_URL",
    "MONGO_URL",
    "MYSQL_PWD",
    "MYSQL_HOME",
    "MYSQL_TEST_LOGIN_FILE",
    "PGPASSFILE",
    "PGPASSWORD",
    "PGSERVICEFILE",
    "POSTGRES_DSN",
    "POSTGRES_URI",
    "POSTGRES_URL",
    "REDIS_TLS_URL",
    "REDIS_URL",
    "REDISCLI_AUTH",
    "SQLCMDPASSWORD",
    "SQLALCHEMY_DATABASE_URI",
    "SSH_AGENT_PID",
    "SSH_AUTH_SOCK",
}
_DATABASE_CREDENTIAL_ENV_NAME = re.compile(
    r"(?:^|_)(?:DATABASE|DB|JDBC_DATABASE|SQLALCHEMY_DATABASE|POSTGRES(?:QL)?|"
    r"MYSQL|MARIADB|MONGO(?:DB)?|REDIS|ELASTICSEARCH|OPENSEARCH)_"
    r"(?:URL|URI|DSN|CONNECTION_STRING)\Z",
    re.IGNORECASE,
)
_CREDENTIAL_URL = re.compile(
    r"[A-Za-z][A-Za-z0-9+.-]*://[^:/@\s]+:[^/@\s]+@",
    re.IGNORECASE,
)
_CONTROLLER_ENV_NAMES = {
    "DOCKER_API_VERSION",
    "DOCKER_CERT_PATH",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_HOST",
    "DOCKER_TLS",
    "DOCKER_TLS_VERIFY",
    "LOOPX_REGISTRY",
    "LOOPX_STATE_FILE",
}
_CONTROLLER_ENV_PREFIXES = ("LOOPX_",)
_EXTERNAL_COMPOSE_PLATFORM_ENV_NAMES = {
    "COMSPEC",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "WINDIR",
}
_EXTERNAL_COMPOSE_DOCKER_ENV_NAMES = {
    "DOCKER_API_VERSION",
    "DOCKER_CERT_PATH",
    "DOCKER_CONTEXT",
    "DOCKER_DEFAULT_PLATFORM",
    "DOCKER_HOST",
    "DOCKER_TLS",
    "DOCKER_TLS_VERIFY",
}
_EXTERNAL_COMPOSE_HOST_ENV_NAMES = (
    _EXTERNAL_COMPOSE_PLATFORM_ENV_NAMES | _EXTERNAL_COMPOSE_DOCKER_ENV_NAMES
)
_EXTERNAL_COMMAND_SERVICE_ENV = {
    "TEST_DIR": ("${LOOPSBENCH_TEST_DIR}", "/tests"),
}
_EXTERNAL_COMMAND_COMMAND = ["sh", "-c", "sleep infinity"]
_EXTERNAL_COMMAND_RENDERED_NETWORK_FIELDS = {
    "expose",
    "external_links",
    "extra_hosts",
    "links",
    "ports",
}
_ALLOWED_TASK_CAPABILITIES = {"NET_ADMIN"}
_ALLOWED_BIND_MOUNT_DESTINATIONS = {"/agent-logs", "/logs"}
_ALLOWED_TASK_VOLUME_DESTINATIONS = {"/shared", "/var/lib/loopsbench/.mirror"}
_TASK_VOLUME_KEY = "loopsbench_shared"
_EXTERNAL_STATIC_SERVICE_MOUNTS = {
    "client": {
        (
            "bind",
            "${LOOPSBENCH_TASK_LOGS_PATH}",
            "/logs",
            False,
        ),
        (
            "bind",
            "${LOOPSBENCH_TASK_AGENT_LOGS_PATH}",
            "/agent-logs",
            False,
        ),
        (
            "volume",
            _TASK_VOLUME_KEY,
            "/var/lib/loopsbench/.mirror",
            False,
        ),
        ("volume", _TASK_VOLUME_KEY, "/shared", False),
    },
    "tester": {
        ("volume", _TASK_VOLUME_KEY, "/shared", False),
        ("bind", "${LOOPSBENCH_TASK_DIR}/tests", "/tests", True),
        (
            "bind",
            "${LOOPSBENCH_TASK_DIR}/run-tests.sh",
            "/task-assets/run-tests.sh",
            True,
        ),
        ("bind", "${LOOPSBENCH_TASK_DIR}", "/task-assets/task", True),
    },
}
_STATIC_MOUNT_TARGETS = {
    "${LOOPSBENCH_CONTAINER_LOGS_PATH}": "/logs",
    "${LOOPSBENCH_CONTAINER_AGENT_LOGS_PATH}": "/agent-logs",
}
_RESTRICTED_MOUNT_ROOTS = {
    "/opt/traex-controller",
    "/opt/traex-profile",
    "/run/configs",
    "/run/secrets",
    "/var/run/configs",
    "/var/run/secrets",
}
_SENSITIVE_MOUNT_PATH = re.compile(
    r"(?:^|/)(?:\.aws|\.kube|\.ssh|configs?|credentials?|secrets?)(?:/|$)",
    re.IGNORECASE,
)
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_EXPECTED_BIND_SOURCES_ATTR = "_loopsbench_expected_bind_sources"
_EXTERNAL_COMMAND_ISOLATION_ATTR = "_loopsbench_external_command_isolation"
_EXPECTED_COMPOSE_PROJECT_ATTR = "_loopsbench_expected_compose_project"
_EXPECTED_COMPOSE_SERVICE_ATTR = "_loopsbench_expected_compose_service"
_EXPECTED_IMAGE_ID_ATTR = "_loopsbench_expected_image_id"
_EXTERNAL_COMMAND_OVERRIDE_FILENAME = "external-command-isolation.compose.yaml"
_EXTERNAL_COMMAND_CLIENT_OVERRIDE = """services:
  client:
    volumes: !reset []
    entrypoint: []
    command: ["sh", "-c", "sleep infinity"]
    healthcheck:
      disable: true
    extra_hosts: !reset []
    expose: !reset []
    external_links: !reset []
    links: !reset []
    ports: !reset []
"""
_EXTERNAL_COMMAND_TESTER_OVERRIDE = """  tester:
    entrypoint: []
    command: ["sh", "-c", "sleep infinity"]
    healthcheck:
      disable: true
    extra_hosts: !reset []
    expose: !reset []
    external_links: !reset []
    links: !reset []
    ports: !reset []
"""
_EXTERNAL_COMMAND_OVERRIDE = (
    _EXTERNAL_COMMAND_CLIENT_OVERRIDE + _EXTERNAL_COMMAND_TESTER_OVERRIDE
)
_EXTERNAL_COMPOSE_INJECTION_FIELDS = {
    "configs",
    "credential_spec",
    "env_file",
    "secrets",
    "volumes_from",
}
# Compose Build Specification fields that can import runner material, alter the
# build namespace or routing, or grant BuildKit an elevated entitlement.  The
# set is intentionally finite and schema-named so new fields remain reviewable.
_EXTERNAL_BUILD_BLOCKED_FIELDS = frozenset(
    {
        "additional_contexts",
        "args",
        "cache_from",
        "cache_to",
        "entitlements",
        "extra_hosts",
        "isolation",
        "network",
        "privileged",
        "secrets",
        "ssh",
    }
)


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------


def docker_cli_prefix() -> list[str]:
    return ["docker"]


def docker_env(include_os_env: bool = True) -> dict[str, str]:
    env = os.environ.copy() if include_os_env else {}
    if os.environ.get("LOOPSBENCH_ROOTFUL_DOCKER", "1") != "0":
        env.pop("DOCKER_HOST", None)
        env.pop("DOCKER_CONTEXT", None)
    return env


def _is_credential_env_name(name: str) -> bool:
    normalized = name.upper()
    return (
        normalized in _CREDENTIAL_ENV_NAMES
        or bool(_CREDENTIAL_ENV_NAME.search(normalized))
        or bool(_DATABASE_CREDENTIAL_ENV_NAME.search(normalized))
    )


def _is_controller_env_name(name: str) -> bool:
    normalized = name.upper()
    return normalized in _CONTROLLER_ENV_NAMES or normalized.startswith(
        _CONTROLLER_ENV_PREFIXES
    )


def _is_credential_env_entry(name: str, value: object) -> bool:
    return _is_credential_env_name(name) or (
        isinstance(value, str) and bool(_CREDENTIAL_URL.match(value))
    )


def _external_command_compose_env(host_env: Mapping[str, str]) -> dict[str, str]:
    """Return the only host values external-command Compose may inherit.

    Compose needs the executable/temporary-directory platform variables and the
    direct Docker endpoint, context, TLS, API, and target-platform selectors.
    It does not need the rest of the controller environment.  In particular,
    HOME, DOCKER_CONFIG, proxy/SSH state, provider credentials, and unknown
    variables stay out of the interpolation namespace.  Manager-created
    LOOPSBENCH_* values are added separately after this filter.
    """
    return {
        name: value
        for name, value in host_env.items()
        if name in _EXTERNAL_COMPOSE_HOST_ENV_NAMES
    }


def _is_external_command_runtime_env(name: str, value: str) -> bool:
    """Accept only bounded, non-secret image/task runtime metadata.

    Container environment has no provenance in Docker inspect, so an
    ever-growing credential-name denylist cannot prove isolation. Each allowed
    name therefore has a constrained public value shape; every unknown name is
    rejected for external-command client and tester containers.
    """
    if name == "TEST_DIR":
        return value == "/tests"
    if name == "PATH":
        return bool(
            value
            and all(
                item.startswith("/")
                and not any(
                    char.isspace() or char in ("=", "$", "\\", '"', "'")
                    for char in item
                )
                for item in value.split(":")
            )
        )
    if name in {"LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TERM", "TZ"}:
        return bool(re.fullmatch(r"[A-Za-z0-9_+./@-]+", value))
    if name in {
        "NODE_VERSION",
        "PYTHON_PIP_VERSION",
        "PYTHON_SETUPTOOLS_VERSION",
        "PYTHON_VERSION",
        "YARN_VERSION",
    }:
        return bool(re.fullmatch(r"[0-9][A-Za-z0-9.+_-]*", value))
    if name in {"GPG_KEY", "PYTHON_GET_PIP_SHA256", "PYTHON_SHA256"}:
        return bool(re.fullmatch(r"[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64}", value))
    if name == "PYTHON_GET_PIP_URL":
        return bool(
            re.fullmatch(
                r"https://github\.com/pypa/get-pip/raw/[0-9A-Fa-f]+/public/get-pip\.py",
                value,
            )
        )
    return False


def _is_external_command_service_env(
    name: str, value: object, *, is_interpolated: bool
) -> bool:
    expected = _EXTERNAL_COMMAND_SERVICE_ENV.get(name)
    if expected is None or not isinstance(value, str):
        return False
    return value == expected[1 if is_interpolated else 0]


def _compose_env_references(value: str) -> set[str]:
    """Return every Compose interpolation reference, including nested ones.

    Compose permits nested substitutions in the operand of its default,
    required, and alternative operators. A flat regex can consume the outer
    expression and hide a sensitive inner reference. This scanner mirrors the
    supported variable grammar and treats malformed braced expressions as an
    admission failure.
    """

    references: set[str] = set()
    operators = (":-", ":?", ":+", "-", "?", "+")

    def scan(index: int, *, nested: bool, depth: int) -> int:
        if depth > 100:
            raise _ComposeInterpolationError(
                "Compose interpolation nesting is too deep"
            )
        while index < len(value):
            char = value[index]
            if nested and char == "}":
                return index + 1
            if char != "$":
                index += 1
                continue
            if index + 1 >= len(value):
                index += 1
                continue
            next_char = value[index + 1]
            if next_char == "$":
                index += 2
                continue
            if next_char == "{":
                index += 2
                name_start = index
                if index >= len(value) or not (
                    value[index].isalpha() or value[index] == "_"
                ):
                    raise _ComposeInterpolationError(
                        "Compose interpolation has no variable name"
                    )
                index += 1
                while index < len(value) and (
                    value[index].isalnum() or value[index] == "_"
                ):
                    index += 1
                references.add(value[name_start:index])
                if index >= len(value):
                    raise _ComposeInterpolationError(
                        "Compose interpolation is unterminated"
                    )
                if value[index] == "}":
                    index += 1
                    continue
                operator = next(
                    (item for item in operators if value.startswith(item, index)),
                    None,
                )
                if operator is None:
                    raise _ComposeInterpolationError(
                        "Compose interpolation uses an unsupported operator"
                    )
                index = scan(index + len(operator), nested=True, depth=depth + 1)
                continue
            if next_char.isalpha() or next_char == "_":
                index += 2
                name_start = index - 1
                while index < len(value) and (
                    value[index].isalnum() or value[index] == "_"
                ):
                    index += 1
                references.add(value[name_start:index])
                continue
            index += 1
        if nested:
            raise _ComposeInterpolationError("Compose interpolation is unterminated")
        return index

    scan(0, nested=False, depth=0)
    return references


def _sensitive_compose_env_references(value: object) -> set[str]:
    if isinstance(value, Mapping):
        items: list[object] = [*value.keys(), *value.values()]
    elif isinstance(value, list):
        items = list(value)
    else:
        items = []
    references: set[str] = set()
    if isinstance(value, str):
        for name in _compose_env_references(value):
            if _is_credential_env_name(name) or _is_controller_env_name(name):
                references.add(name)
    for item in items:
        references.update(_sensitive_compose_env_references(item))
    return references


def _canonical_bind_source(source: str) -> str:
    """Normalize Docker Desktop's VM prefix for host-path comparison only."""
    if source.startswith("/host_mnt/"):
        return source.removeprefix("/host_mnt")
    return source


def docker_command(args: list[str]) -> list[str]:
    return [*docker_cli_prefix(), *args]


# ---------------------------------------------------------------------------
# DockerComposeManager
# ---------------------------------------------------------------------------


class DockerComposeManager:
    """Manages the lifecycle of Docker Compose services for a single task."""

    CONTAINER_LOGS_PATH = "/logs"
    CONTAINER_AGENT_LOGS_PATH = "/agent-logs"
    CONTAINER_TEST_DIR = Path("/tests")

    def __init__(
        self,
        client_container_name: str,
        client_image_name: str,
        docker_compose_path: Path | Sequence[Path],
        docker_image_name_prefix: str | None = None,
        tester_container_name: str | None = None,
        tester_image_name: str | None = None,
        docker_image_strategy: DockerImageStrategy | None = None,
        no_rebuild: bool = False,
        cleanup: bool = False,
        logs_path: Path | None = None,
        agent_logs_path: Path | None = None,
        task_dir: Path | str | None = None,
        external_command_isolation: bool = False,
    ):
        docker_module = _get_docker_module()
        try:
            self._client = docker_module.from_env(environment=docker_env())
        except docker_module.errors.DockerException as exc:
            raise RuntimeError(
                f"Error creating docker client: {exc}. "
                "Please ensure that Docker is installed and running."
            )

        self._client_container_name = client_container_name
        self._client_image_name = client_image_name
        self._tester_container_name = tester_container_name
        self._tester_image_name = tester_image_name
        self._docker_name_prefix = docker_image_name_prefix
        if isinstance(docker_compose_path, (str, Path)):
            self._compose_files = [Path(docker_compose_path)]
        else:
            self._compose_files = [Path(p) for p in docker_compose_path]
        self._external_command_override_dir: tempfile.TemporaryDirectory[str] | None = (
            None
        )
        self._docker_image_strategy = docker_image_strategy
        if self._docker_image_strategy is None:
            self._docker_image_strategy = (
                DockerImageStrategy.LOCAL_EXISTING
                if no_rebuild
                else DockerImageStrategy.LOCAL_BUILD
            )
        self._no_rebuild = no_rebuild
        self._cleanup = cleanup
        self._client_container: Container | None = None
        self._tester_container: Container | None = None
        # Cleanup identity is intentionally weaker than admission evidence: it
        # records only the immutable ID returned by the runner's service lookup
        # so a rejected generation can still be removed without ever trusting a
        # later same-name occupant.
        self._cleanup_client_container_id: str | None = None
        self._cleanup_tester_container_id: str | None = None
        self._cleanup_default_network_id: str | None = None
        self._admitted_client: EffectiveContainerEvidence | None = None
        self._admitted_tester: EffectiveContainerEvidence | None = None
        self._retired_external_services: set[str] = set()
        self._logs_path = logs_path
        self._agent_logs_path = agent_logs_path
        self._task_dir = Path(task_dir).resolve() if task_dir is not None else None
        self._external_command_isolation = external_command_isolation
        self._logger = logger.getChild(__name__)
        self._image_resolution = DockerImageResolution(
            requested_client_image_ref=self._client_image_name
        )

        compose_env = DockerComposeEnvVars(
            task_docker_client_image_name=self._client_image_name,
            task_docker_client_container_name=self._client_container_name,
            task_docker_tester_image_name=self._tester_image_name,
            task_docker_tester_container_name=self._tester_container_name,
            task_docker_name_prefix=self._docker_name_prefix,
            container_logs_path=self.CONTAINER_LOGS_PATH,
            container_agent_logs_path=self.CONTAINER_AGENT_LOGS_PATH,
            test_dir=str(self.CONTAINER_TEST_DIR),
            task_logs_path=(
                str(self._logs_path.absolute()) if self._logs_path is not None else None
            ),
            task_agent_logs_path=(
                str(self._agent_logs_path.absolute())
                if self._agent_logs_path is not None
                else None
            ),
            task_dir=str(self._task_dir) if self._task_dir is not None else None,
        ).to_env_dict(include_os_env=False)
        host_env = docker_env()
        if self._external_command_isolation:
            host_env = _external_command_compose_env(host_env)
        self.env = {**host_env, **compose_runtime_env(compose_env)}
        if self._external_command_isolation:
            self._external_command_override_dir = tempfile.TemporaryDirectory(
                prefix="loopsbench-external-compose-"
            )
            override_path = (
                Path(self._external_command_override_dir.name)
                / _EXTERNAL_COMMAND_OVERRIDE_FILENAME
            )
            override = _EXTERNAL_COMMAND_CLIENT_OVERRIDE
            if self._tester_container_name is not None:
                override += _EXTERNAL_COMMAND_TESTER_OVERRIDE
            override_path.write_text(override, encoding="utf-8")
            self._compose_files.append(override_path)

    def _record_expected_bind_sources(self, container: Container) -> None:
        expected_sources = {
            destination: str(path.absolute())
            for destination, path in (
                (self.CONTAINER_LOGS_PATH, self._logs_path),
                (self.CONTAINER_AGENT_LOGS_PATH, self._agent_logs_path),
            )
            if path is not None
        }
        setattr(container, _EXPECTED_BIND_SOURCES_ATTR, expected_sources)
        setattr(
            container,
            _EXTERNAL_COMMAND_ISOLATION_ATTR,
            self._external_command_isolation,
        )

    def _record_external_container_identity(
        self, container: Container, *, service_name: str
    ) -> str:
        container_id = self._cleanup_container_id(container)
        setattr(
            container,
            _EXPECTED_COMPOSE_PROJECT_ATTR,
            self._client_container_name.lower(),
        )
        setattr(container, _EXPECTED_COMPOSE_SERVICE_ATTR, service_name)
        setattr(container, _EXPECTED_IMAGE_ID_ATTR, self._external_image_id())
        return container_id

    def _external_image_id(self) -> str:
        image_id = self._image_resolution.resolved_image_id
        if not isinstance(image_id, str) or not image_id:
            raise ContainerAdmissionError(
                "external_client_image_resolution_unavailable"
            )
        return image_id

    def _prepare_external_container_for_inspection(
        self, container: Container, *, service_name: str
    ) -> str:
        self._record_expected_bind_sources(container)
        return self._record_external_container_identity(
            container, service_name=service_name
        )

    # -- compose commands --

    def _compose_cmd(self, command: list[str]) -> list[str]:
        compose_file_args: list[str] = []
        for compose_file in self._compose_files:
            compose_file_args.extend(["-f", str(compose_file.resolve().absolute())])
        return docker_command(
            [
                "compose",
                *(
                    ["--env-file", os.devnull]
                    if self._external_command_isolation
                    else []
                ),
                "-p",
                self._client_container_name.lower(),
                *compose_file_args,
                *command,
            ]
        )

    @staticmethod
    def _cleanup_container_id(container: Container) -> str:
        attrs = getattr(container, "attrs", None)
        container_id = (
            attrs.get("Id") if isinstance(attrs, Mapping) else None
        ) or getattr(container, "id", None)
        if not isinstance(container_id, str) or not container_id:
            raise ContainerAdmissionError(
                "external_container_cleanup_identity_unavailable"
            )
        return container_id

    def _run_compose(self, command: list[str]) -> subprocess.CompletedProcess:
        full = self._compose_cmd(command)
        self._logger.debug(f"Running: {' '.join(full)}")
        try:
            return subprocess.run(
                full,
                env=self.env,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            tail = 4000
            self._logger.warning(
                f"Docker compose failed (exit {exc.returncode}): {' '.join(full)}"
            )
            if exc.stdout:
                self._logger.warning(
                    f"compose stdout (tail {tail}): {(exc.stdout or '')[-tail:]}"
                )
            if exc.stderr:
                self._logger.warning(
                    f"compose stderr (tail {tail}): {(exc.stderr or '')[-tail:]}"
                )
            raise

    # -- lifecycle --

    def _service_container(
        self, service: str, retries: int = 60, delay_sec: float = 0.5
    ) -> Container | None:
        for _ in range(retries):
            result = subprocess.run(
                self._compose_cmd(["ps", "-q", service]),
                env=self.env,
                capture_output=True,
                text=True,
            )
            container_id = (result.stdout or "").strip().splitlines()
            if container_id:
                try:
                    return self._client.containers.get(container_id[0])
                except _get_docker_module().errors.NotFound:
                    pass
            time.sleep(delay_sec)
        return None

    def _compose_service_ids(self) -> dict[str, str]:
        """Return immutable IDs for the exact services owned by this project."""
        expected_services = {"client"}
        if self._tester_container_name is not None:
            expected_services.add("tester")
        service_ids: dict[str, str] = {}
        for service in sorted(expected_services):
            try:
                result = subprocess.run(
                    self._compose_cmd(["ps", "--all", "-q", service]),
                    env=self.env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except (OSError, subprocess.CalledProcessError) as exc:
                raise ContainerAdmissionError(
                    "external_container_cleanup_identity_inspection_unavailable"
                ) from exc
            ids = [value.strip() for value in (result.stdout or "").splitlines()]
            ids = [value for value in ids if value]
            if len(ids) > 1:
                raise ContainerAdmissionError(
                    f"external_container_cleanup_identity_ambiguous: {service}"
                )
            if ids:
                service_ids[service] = ids[0]
        return service_ids

    def _prove_external_service_container(
        self, *, service_name: str, container_id: str
    ) -> Container:
        """Resolve one raw Compose ID and prove exact project/service ownership."""
        try:
            container = self._client.containers.get(container_id)
            container.reload()
            attrs = container.attrs
        except Exception as exc:
            raise ContainerAdmissionError(
                f"external_container_cleanup_identity_unproven: {service_name}"
            ) from exc
        config = attrs.get("Config") if isinstance(attrs, Mapping) else None
        labels = config.get("Labels") if isinstance(config, Mapping) else None
        if (
            not isinstance(labels, Mapping)
            or labels.get("com.docker.compose.project")
            != self._client_container_name.lower()
            or labels.get("com.docker.compose.service") != service_name
            or self._cleanup_container_id(container) != container_id
        ):
            raise ContainerAdmissionError(
                f"external_container_cleanup_identity_unproven: {service_name}"
            )
        return container

    def _capture_external_cleanup_identities(self) -> None:
        """Capture only label-proven IDs, including after a partial `up`."""
        self._capture_external_default_network_identity()
        service_ids = self._compose_service_ids()
        for service_name, container_id in service_ids.items():
            container = self._prove_external_service_container(
                service_name=service_name, container_id=container_id
            )
            if service_name == "client":
                self._client_container = container
                self._cleanup_client_container_id = container_id
            elif service_name == "tester":
                self._tester_container = container
                self._cleanup_tester_container_id = container_id

    def _capture_external_default_network_identity(self) -> None:
        network_name = f"{self._client_container_name.lower()}_default"
        not_found = _get_docker_module().errors.NotFound
        try:
            network = self._client.networks.get(network_name)
            network.reload()
            attrs = network.attrs
        except not_found:
            return
        except Exception as exc:
            raise ContainerAdmissionError(
                "external_network_cleanup_identity_inspection_unavailable"
            ) from exc
        labels = attrs.get("Labels") if isinstance(attrs, Mapping) else None
        network_id = attrs.get("Id") if isinstance(attrs, Mapping) else None
        if (
            not isinstance(labels, Mapping)
            or labels.get("com.docker.compose.project")
            != self._client_container_name.lower()
            or labels.get("com.docker.compose.network") != "default"
            or not isinstance(network_id, str)
            or not network_id
        ):
            raise ContainerAdmissionError("external_network_cleanup_identity_unproven")
        self._cleanup_default_network_id = network_id

    def _run_external_compose_up(self, command: list[str]) -> None:
        try:
            self._run_compose(command)
        except Exception:
            try:
                self._capture_external_cleanup_identities()
            except ContainerAdmissionError as identity_exc:
                raise identity_exc
            raise
        self._capture_external_cleanup_identities()

    def _run_docker(self, command: list[str]) -> subprocess.CompletedProcess:
        full = docker_command(command)
        self._logger.debug(f"Running: {' '.join(full)}")
        return subprocess.run(
            full,
            env=docker_env(),
            check=True,
            capture_output=True,
            text=True,
        )

    def _validate_task_compose_security(self) -> None:
        if not self._compose_files:
            return
        issues = validate_compose_security(self._compose_files[0])
        if issues:
            error_type = (
                ContainerAdmissionError
                if self._external_command_isolation
                else RuntimeError
            )
            raise error_type(
                "Unsafe Docker Compose task configuration:\n"
                f"{format_compose_security_issues(issues)}"
            )
        if self._external_command_isolation:
            for compose_path in self._compose_files[:-1]:
                self._validate_external_command_preprovisioning(compose_path)
            try:
                # This is both effective-config admission and the fail-closed
                # compatibility probe for the generated `!reset` override. It
                # must succeed before Compose is allowed to build or start.
                rendered = self._run_compose(["config", "--format", "json"])
                payload = json.loads(rendered.stdout)
            except (
                subprocess.CalledProcessError,
                json.JSONDecodeError,
                OSError,
                TypeError,
            ) as exc:
                raise ContainerAdmissionError(
                    "External-command Compose interpolation inspection unavailable."
                ) from exc
            self._validate_external_command_compose_payload(
                payload,
                source="interpolated Compose config",
                is_interpolated=True,
            )

    @staticmethod
    def _compose_env_entries(value: object) -> list[tuple[str, object]]:
        if isinstance(value, Mapping):
            return [(str(name), env_value) for name, env_value in value.items()]
        if not isinstance(value, list):
            return []
        entries: list[tuple[str, object]] = []
        for item in value:
            if not isinstance(item, str) or not item:
                continue
            name, separator, env_value = item.partition("=")
            entries.append((name, env_value if separator else None))
        return entries

    def _validate_external_command_preprovisioning(self, compose_path: Path) -> None:
        """Reject task-owned injection channels before Compose can provision."""
        try:
            payload = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ContainerAdmissionError(
                "External-command task Compose inspection unavailable."
            ) from exc
        self._validate_external_command_compose_payload(
            payload,
            source="task Compose",
            is_interpolated=False,
        )

    @staticmethod
    def _external_command_mount(
        value: object,
        *,
        source: str,
        field: str,
        is_interpolated: bool,
    ) -> tuple[str, str, str, bool]:
        if isinstance(value, str) and not is_interpolated:
            parts = value.split(":")
            if len(parts) not in (2, 3) or (
                len(parts) == 3 and parts[2] not in {"ro", "rw"}
            ):
                raise ContainerAdmissionError(
                    f"External-command {source} mount inspection unavailable at "
                    f"{field}."
                )
            mount_source, target = parts[:2]
            target = _STATIC_MOUNT_TARGETS.get(target, target)
            mount_type = "volume" if mount_source == _TASK_VOLUME_KEY else "bind"
            return (
                mount_type,
                mount_source,
                target,
                len(parts) == 3 and parts[2] == "ro",
            )
        if not isinstance(value, Mapping):
            raise ContainerAdmissionError(
                f"External-command {source} mount inspection unavailable at {field}."
            )
        if not is_interpolated and set(value) - {
            "type",
            "source",
            "target",
            "read_only",
        }:
            raise ContainerAdmissionError(
                f"External-command {source} mount inspection unavailable at {field}."
            )
        mount_type = value.get("type")
        mount_source = value.get("source")
        target = value.get("target")
        read_only = value.get("read_only", False)
        if (
            not isinstance(mount_type, str)
            or not isinstance(mount_source, str)
            or not isinstance(target, str)
            or not isinstance(read_only, bool)
        ):
            raise ContainerAdmissionError(
                f"External-command {source} mount inspection unavailable at {field}."
            )
        return (
            mount_type.lower(),
            mount_source,
            _STATIC_MOUNT_TARGETS.get(target, target),
            read_only,
        )

    def _external_command_allowed_rendered_mounts(
        self, service_name: str
    ) -> set[tuple[str, str, str, bool]]:
        if service_name != "tester":
            return set()
        mounts = {("volume", _TASK_VOLUME_KEY, "/shared", False)}
        task_dir = self.env.get("LOOPSBENCH_TASK_DIR")
        if task_dir is not None:
            mounts.update(
                {
                    ("bind", str(Path(task_dir) / "tests"), "/tests", True),
                    (
                        "bind",
                        str(Path(task_dir) / "run-tests.sh"),
                        "/task-assets/run-tests.sh",
                        True,
                    ),
                    ("bind", task_dir, "/task-assets/task", True),
                }
            )
        return mounts

    def _validate_external_command_service_mounts(
        self,
        *,
        service_name: str,
        volumes: object,
        source: str,
        is_interpolated: bool,
        unsafe_fields: set[str],
    ) -> None:
        if volumes is None:
            volumes = []
        if not isinstance(volumes, list):
            raise ContainerAdmissionError(
                f"External-command {source} mount inspection unavailable at "
                f"services.{service_name}.volumes."
            )
        actual_mounts: list[tuple[str, str, str, bool]] = []
        for index, volume in enumerate(volumes):
            field = f"services.{service_name}.volumes[{index}]"
            mount = self._external_command_mount(
                volume,
                source=source,
                field=field,
                is_interpolated=is_interpolated,
            )
            actual_mounts.append(mount)

        allowed_mounts = (
            self._external_command_allowed_rendered_mounts(service_name)
            if is_interpolated
            else _EXTERNAL_STATIC_SERVICE_MOUNTS.get(service_name, set())
        )
        for index, mount in enumerate(actual_mounts):
            if mount not in allowed_mounts:
                unsafe_fields.add(f"services.{service_name}.volumes[{index}]")
        if len(actual_mounts) != len(set(actual_mounts)):
            unsafe_fields.add(f"services.{service_name}.volumes")

        if is_interpolated and service_name == "client" and actual_mounts:
            unsafe_fields.add("services.client.volumes")
        if (
            is_interpolated
            and service_name == "tester"
            and self._tester_container_name is not None
            and set(actual_mounts) != allowed_mounts
        ):
            unsafe_fields.add("services.tester.volumes")

    @staticmethod
    def _validate_external_command_rendered_service_security(
        *,
        service_name: str,
        service: Mapping[str, object],
        unsafe_fields: set[str],
    ) -> None:
        """Validate isolation settings after Compose interpolation and merge."""
        for field in ("privileged", "use_api_socket"):
            value = service.get(field)
            if value is not None and not isinstance(value, bool):
                raise ContainerAdmissionError(
                    "External-command interpolated Compose config inspection "
                    f"unavailable at services.{service_name}.{field}."
                )
            if value is True:
                unsafe_fields.add(f"services.{service_name}.{field}")

        cap_add = service.get("cap_add")
        if cap_add is None:
            cap_add = []
        if not isinstance(cap_add, list) or not all(
            isinstance(item, str) and item.strip() for item in cap_add
        ):
            raise ContainerAdmissionError(
                "External-command interpolated Compose config inspection "
                f"unavailable at services.{service_name}.cap_add."
            )
        unexpected_caps = {
            item.strip().upper().removeprefix("CAP_") for item in cap_add
        } - _ALLOWED_TASK_CAPABILITIES
        if unexpected_caps:
            unsafe_fields.add(f"services.{service_name}.cap_add")

        for field in ("devices", "device_cgroup_rules", "gpus"):
            value = service.get(field)
            if value not in (None, [], {}):
                unsafe_fields.add(f"services.{service_name}.{field}")

        security_opt = service.get("security_opt")
        if security_opt is None:
            security_opt = []
        if not isinstance(security_opt, list) or not all(
            isinstance(item, str) for item in security_opt
        ):
            raise ContainerAdmissionError(
                "External-command interpolated Compose config inspection "
                f"unavailable at services.{service_name}.security_opt."
            )
        unsafe_security_opts = {
            "apparmor:unconfined",
            "seccomp:unconfined",
            "label:disable",
        }
        if any(
            item.strip().lower().replace("=", ":", 1) in unsafe_security_opts
            for item in security_opt
        ):
            unsafe_fields.add(f"services.{service_name}.security_opt")

        runtime = service.get("runtime")
        if runtime not in (None, "", "runc"):
            unsafe_fields.add(f"services.{service_name}.runtime")

        deploy = service.get("deploy")
        if deploy is not None and not isinstance(deploy, Mapping):
            raise ContainerAdmissionError(
                "External-command interpolated Compose config inspection "
                f"unavailable at services.{service_name}.deploy."
            )
        resources = deploy.get("resources") if isinstance(deploy, Mapping) else None
        if resources is not None and not isinstance(resources, Mapping):
            raise ContainerAdmissionError(
                "External-command interpolated Compose config inspection "
                f"unavailable at services.{service_name}.deploy.resources."
            )
        reservations = (
            resources.get("reservations") if isinstance(resources, Mapping) else None
        )
        if reservations is not None and not isinstance(reservations, Mapping):
            raise ContainerAdmissionError(
                "External-command interpolated Compose config inspection "
                f"unavailable at services.{service_name}.deploy.resources.reservations."
            )
        if isinstance(reservations, Mapping) and reservations.get("devices") not in (
            None,
            [],
            {},
        ):
            unsafe_fields.add(
                f"services.{service_name}.deploy.resources.reservations.devices"
            )

    def _validate_external_command_service_networks(
        self,
        *,
        service_name: str,
        service: Mapping[str, object],
        source: str,
        is_interpolated: bool,
        unsafe_fields: set[str],
    ) -> None:
        networks = service.get("networks")
        if networks is None:
            network_names: set[str] = set()
        elif isinstance(networks, list) and all(
            isinstance(item, str) and item for item in networks
        ):
            network_names = set(networks)
        elif isinstance(networks, Mapping) and all(
            isinstance(name, str) and name for name in networks
        ):
            network_names = set(networks)
        else:
            raise ContainerAdmissionError(
                f"External-command {source} network inspection unavailable at "
                f"services.{service_name}.networks."
            )
        if network_names - {"default"}:
            unsafe_fields.add(f"services.{service_name}.networks")
        if networks is not None:
            if not isinstance(networks, Mapping) or set(networks) != {"default"}:
                unsafe_fields.add(f"services.{service_name}.networks")
            elif networks.get("default") not in (None, {}):
                unsafe_fields.add(f"services.{service_name}.networks.default")

    def _validate_external_command_compose_payload(
        self,
        payload: object,
        *,
        source: str,
        is_interpolated: bool,
    ) -> None:
        if not isinstance(payload, Mapping):
            raise ContainerAdmissionError(
                f"External-command {source} inspection unavailable."
            )
        unsafe_fields: set[str] = set()
        if not is_interpolated:
            try:
                references = _sensitive_compose_env_references(payload)
            except _ComposeInterpolationError as exc:
                raise ContainerAdmissionError(
                    f"External-command {source} interpolation inspection unavailable."
                ) from exc
            unsafe_fields.update(f"interpolation.{name}" for name in sorted(references))
        for field in ("configs", "secrets"):
            if field in payload:
                unsafe_fields.add(field)
        top_level_networks = payload.get("networks")
        if top_level_networks is not None:
            if not isinstance(top_level_networks, Mapping):
                raise ContainerAdmissionError(
                    f"External-command {source} network inspection unavailable."
                )
            expected_network = f"{self._client_container_name.lower()}_default"
            for network_name, network_config in top_level_networks.items():
                field = f"networks.{network_name}"
                if network_name != "default" or not isinstance(network_config, Mapping):
                    unsafe_fields.add(field)
                    continue
                if is_interpolated:
                    # Compose materializes an empty IPAM object for its own
                    # project-local default network. Keep that generated shape
                    # admissible without accepting task-selected drivers,
                    # external networks, labels, or address configuration.
                    if (
                        set(network_config) - {"name", "ipam"}
                        or network_config.get("name") != expected_network
                        or network_config.get("ipam") not in (None, {})
                    ):
                        unsafe_fields.add(field)
                elif network_config not in (None, {}):
                    unsafe_fields.add(field)
        top_level_volumes = payload.get("volumes")
        if top_level_volumes is not None:
            if not isinstance(top_level_volumes, Mapping):
                raise ContainerAdmissionError(
                    f"External-command {source} inspection unavailable."
                )
            for volume_name, volume_config in top_level_volumes.items():
                if volume_name != _TASK_VOLUME_KEY or (
                    not is_interpolated and volume_config not in (None, {})
                ):
                    unsafe_fields.add(f"volumes.{volume_name}")
        services = payload.get("services")
        if not isinstance(services, Mapping):
            raise ContainerAdmissionError(
                f"External-command {source} inspection unavailable."
            )
        service_names = {str(name) for name in services}
        allowed_services = {"client"}
        if self._tester_container_name is not None:
            allowed_services.add("tester")
        unexpected_services = service_names - allowed_services
        unsafe_fields.update(
            f"services.{service_name}" for service_name in sorted(unexpected_services)
        )
        if is_interpolated and service_names != allowed_services:
            missing_services = allowed_services - service_names
            unsafe_fields.update(
                f"services.{service_name}" for service_name in sorted(missing_services)
            )
        if is_interpolated and not isinstance(services.get("client"), Mapping):
            raise ContainerAdmissionError(
                f"External-command {source} inspection unavailable."
            )
        if (
            is_interpolated
            and self._tester_container_name is not None
            and not isinstance(services.get("tester"), Mapping)
        ):
            raise ContainerAdmissionError(
                f"External-command {source} inspection unavailable."
            )
        for raw_service_name, service in services.items():
            service_name = str(raw_service_name)
            if not isinstance(service, Mapping):
                raise ContainerAdmissionError(
                    f"External-command {source} inspection unavailable."
                )
            self._validate_external_command_service_mounts(
                service_name=service_name,
                volumes=service.get("volumes"),
                source=source,
                is_interpolated=is_interpolated,
                unsafe_fields=unsafe_fields,
            )
            for field in _EXTERNAL_COMPOSE_INJECTION_FIELDS:
                if field in service:
                    unsafe_fields.add(f"services.{service_name}.{field}")
            if is_interpolated:
                self._validate_external_command_rendered_service_security(
                    service_name=service_name,
                    service=service,
                    unsafe_fields=unsafe_fields,
                )
                namespace_modes = {
                    "cgroup": service.get("cgroup"),
                    "ipc": service.get("ipc"),
                    "network_mode": service.get("network_mode"),
                    "pid": service.get("pid"),
                    "userns_mode": service.get("userns_mode"),
                    "uts": service.get("uts"),
                }
                if namespace_modes["cgroup"] not in (None, "", "private"):
                    unsafe_fields.add(f"services.{service_name}.cgroup")
                if namespace_modes["ipc"] not in (None, "", "private"):
                    unsafe_fields.add(f"services.{service_name}.ipc")
                if namespace_modes["network_mode"] not in (
                    None,
                    "",
                    "default",
                    "none",
                ):
                    unsafe_fields.add(f"services.{service_name}.network_mode")
                for field in ("pid", "userns_mode", "uts"):
                    if namespace_modes[field] not in (None, ""):
                        unsafe_fields.add(f"services.{service_name}.{field}")
                for field in _EXTERNAL_COMMAND_RENDERED_NETWORK_FIELDS:
                    if service.get(field) not in (None, [], {}):
                        unsafe_fields.add(f"services.{service_name}.{field}")
                if service.get("entrypoint") not in (None, []):
                    unsafe_fields.add(f"services.{service_name}.entrypoint")
                if service.get("command") != _EXTERNAL_COMMAND_COMMAND:
                    unsafe_fields.add(f"services.{service_name}.command")
                healthcheck = service.get("healthcheck")
                if not (
                    isinstance(healthcheck, Mapping)
                    and healthcheck.get("disable") is True
                    and set(healthcheck) == {"disable"}
                ):
                    unsafe_fields.add(f"services.{service_name}.healthcheck")
                for field in ("post_start", "pre_stop"):
                    if service.get(field) not in (None, []):
                        unsafe_fields.add(f"services.{service_name}.{field}")
            else:
                if service.get("network_mode") not in (
                    None,
                    "",
                    "default",
                    "none",
                ):
                    unsafe_fields.add(f"services.{service_name}.network_mode")
                for field in ("post_start", "pre_stop"):
                    if field in service:
                        unsafe_fields.add(f"services.{service_name}.{field}")
            if is_interpolated and service_name in allowed_services:
                if service.get("image") != self._client_image_name:
                    unsafe_fields.add(f"services.{service_name}.image")
            elif service_name == "tester" and self._tester_container_name is not None:
                image = service.get("image")
                allowed_tester_images = {self._client_image_name}
                allowed_tester_images.add("${LOOPSBENCH_TASK_DOCKER_CLIENT_IMAGE_NAME}")
                if self._tester_image_name == self._client_image_name:
                    allowed_tester_images.add(
                        "${LOOPSBENCH_TASK_DOCKER_TESTER_IMAGE_NAME}"
                    )
                if image is not None and image not in allowed_tester_images:
                    unsafe_fields.add("services.tester.image")
            self._validate_external_command_service_networks(
                service_name=service_name,
                service=service,
                source=source,
                is_interpolated=is_interpolated,
                unsafe_fields=unsafe_fields,
            )
            environment = service.get("environment")
            if environment is not None and not isinstance(environment, (Mapping, list)):
                raise ContainerAdmissionError(
                    f"External-command {source} inspection unavailable."
                )
            env_entries = self._compose_env_entries(environment)
            unsafe_env_names = sorted(
                name
                for name, value in env_entries
                if not _is_external_command_service_env(
                    name, value, is_interpolated=is_interpolated
                )
            )
            unsafe_fields.update(
                f"services.{service_name}.environment.{name}"
                for name in unsafe_env_names
            )
            build = service.get("build")
            if isinstance(build, Mapping):
                for field in _EXTERNAL_BUILD_BLOCKED_FIELDS:
                    if field in build:
                        unsafe_fields.add(f"services.{service_name}.build.{field}")
        if unsafe_fields:
            joined = ", ".join(sorted(unsafe_fields))
            raise ContainerAdmissionError(
                f"Unsafe {source} configuration for external-command "
                f"pre-provisioning: {joined}"
            )

    def _pull_remote_image(self) -> None:
        try:
            self._run_docker(["pull", self._client_image_name])
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            detail_suffix = f" Detail: {detail}" if detail else ""
            raise RuntimeError(
                f"Failed to pull remote task image {self._client_image_name}. "
                "Verify the image exists and that Docker can authenticate to the "
                f"registry.{detail_suffix}"
            ) from exc

    def _ensure_local_image_exists(self) -> None:
        try:
            self._run_docker(["image", "inspect", self._client_image_name])
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            detail_suffix = f" Detail: {detail}" if detail else ""
            raise RuntimeError(
                f"Task image {self._client_image_name} is not present locally for "
                "docker_image_strategy=local-existing. Use docker_image_strategy=remote, "
                "use docker_image_strategy=local-build, or pre-pull/tag the image "
                f"before running.{detail_suffix}"
            ) from exc

    @staticmethod
    def _image_repo(image_ref: str) -> str:
        if "@" in image_ref:
            return image_ref.split("@", 1)[0]
        last_slash = image_ref.rfind("/")
        last_colon = image_ref.rfind(":")
        if last_colon > last_slash:
            return image_ref[:last_colon]
        return image_ref

    @classmethod
    def _select_repo_digest(
        cls,
        image_ref: str,
        repo_digests: Sequence[str],
    ) -> str | None:
        if not repo_digests:
            return None
        requested_repo = cls._image_repo(image_ref)
        requested_prefix = f"{requested_repo}@"
        for repo_digest in repo_digests:
            if repo_digest.startswith(requested_prefix):
                return repo_digest
        return None

    def _capture_image_resolution(self) -> None:
        try:
            result = self._run_docker(
                [
                    "image",
                    "inspect",
                    "--format",
                    "{{json .}}",
                    self._client_image_name,
                ]
            )
            payload = json.loads(result.stdout or "{}")
            if not isinstance(payload, Mapping):
                raise ValueError("docker image inspect returned a non-object payload")
            image_id = payload.get("Id")
            raw_repo_digests = payload.get("RepoDigests") or []
            repo_digests = [
                repo_digest
                for repo_digest in raw_repo_digests
                if isinstance(repo_digest, str)
            ]
            self._image_resolution = DockerImageResolution(
                requested_client_image_ref=self._client_image_name,
                resolved_image_id=image_id if isinstance(image_id, str) else None,
                resolved_repo_digest=self._select_repo_digest(
                    self._client_image_name,
                    repo_digests,
                ),
            )
        except Exception as exc:
            self._logger.warning(
                "Failed to capture docker image resolution for "
                f"{self._client_image_name}: {exc}"
            )

    def start(self) -> Container:
        self._validate_task_compose_security()
        if self._docker_image_strategy == DockerImageStrategy.REMOTE:
            self._pull_remote_image()
            self._capture_image_resolution()
            up_command = ["up", "-d", "--no-build"]
        elif self._docker_image_strategy == DockerImageStrategy.LOCAL_BUILD:
            self._run_compose(["build"])
            self._capture_image_resolution()
            up_command = ["up", "-d"]
        else:
            self._ensure_local_image_exists()
            self._capture_image_resolution()
            up_command = ["up", "-d", "--no-build"]
        if self._external_command_isolation:
            # Compose can fail after creating only part of the stack. Capture
            # every label-proven service ID before propagating that failure so
            # teardown never has to select a later same-name generation.
            self._run_external_compose_up(up_command)
        else:
            self._run_compose(up_command)
        if self._client_container is None:
            self._client_container = self._service_container("client")
        if self._client_container is None:
            raise RuntimeError(
                f"Client container did not start for compose project {self._client_container_name.lower()}"
            )
        if self._external_command_isolation:
            self._cleanup_client_container_id = (
                self._prepare_external_container_for_inspection(
                    self._client_container, service_name="client"
                )
            )
        else:
            self._record_expected_bind_sources(self._client_container)
        if self._tester_container_name:
            if self._tester_container is None:
                self._tester_container = self._service_container("tester")
            if self._tester_container is None:
                raise RuntimeError(
                    f"Tester container did not start for compose project "
                    f"{self._client_container_name.lower()} "
                    f"(expected name {self._tester_container_name!r}). "
                    "Check that the compose file declares a `tester` service "
                    "and that LOOPSBENCH_TASK_DOCKER_TESTER_CONTAINER_NAME matches "
                    "the container_name template in the YAML."
                )
            if self._external_command_isolation:
                self._cleanup_tester_container_id = (
                    self._prepare_external_container_for_inspection(
                        self._tester_container, service_name="tester"
                    )
                )
        if self._tester_container is not None and self._client_container is not None:
            # Legacy: we used to symlink /workspace/regression_status.json ->
            # /shared/feedback/regression_status.json so the agent could read
            # live feedback. That leaked the /shared plumbing into the agent's
            # view; regression status is now host-only. Intentionally no-op.
            pass
        if self._external_command_isolation:
            self._admitted_client = None
            self._admitted_tester = None
            try:
                self._admitted_client = self.inspect_effective_container(
                    self._client_container,
                    external_command_isolation=True,
                    expected_compose_project=self._client_container_name.lower(),
                    expected_compose_service="client",
                )
                if self._tester_container is not None:
                    self._admitted_tester = self._inspect_external_tester(
                        self._tester_container
                    )
            except ContainerAdmissionError:
                # Admission evidence remains empty for a rejected generation.
                # Teardown is independently authorized by the immutable IDs
                # captured from the runner's Compose service lookup above.
                raise
        if self._client_container is not None and not self._external_command_isolation:
            try:
                # Ensure git exists, then initialise a repo in /workspace so
                # diff-based progress tracking (regression_harness) can measure
                # the agent's work. Many base images lack git by default.
                rc, out = self._client_container.exec_run(
                    "sh -c '"
                    "command -v git >/dev/null 2>&1 || "
                    "(command -v apt-get >/dev/null 2>&1 && "
                    " (apt-get update -qq && apt-get install -y -qq git >/dev/null)) || "
                    "(command -v apk >/dev/null 2>&1 && apk add --no-cache git >/dev/null) || "
                    "(command -v yum >/dev/null 2>&1 && yum install -y -q git >/dev/null) || "
                    "(command -v microdnf >/dev/null 2>&1 && microdnf install -y git >/dev/null); "
                    "command -v git >/dev/null 2>&1 && "
                    "mkdir -p /var/lib/loopsbench && "
                    "rm -rf /var/lib/loopsbench/git && "
                    "cd /workspace && "
                    "rm -rf .git && "
                    "GIT_DIR=/var/lib/loopsbench/git GIT_WORK_TREE=/workspace "
                    "  git init -q && "
                    "GIT_DIR=/var/lib/loopsbench/git GIT_WORK_TREE=/workspace "
                    "  git -c user.email=b@b -c user.name=b add -A && "
                    "GIT_DIR=/var/lib/loopsbench/git GIT_WORK_TREE=/workspace "
                    "  git -c user.email=b@b -c user.name=b commit -q -m base --allow-empty'"
                )
                if rc != 0:
                    self._logger.warning(
                        f"workspace git bootstrap failed (rc={rc}): "
                        f"{(out or b'').decode(errors='replace')[:300]}"
                    )
            except Exception as exc:
                self._logger.warning(f"git init in workspace failed: {exc}")
        return self._client_container

    @staticmethod
    def inspect_effective_container(
        container: Container,
        *,
        external_command_isolation: bool | None = None,
        expected_mounts: set[tuple[str, str, str, bool]] | None = None,
        expected_compose_project: str | None = None,
        expected_compose_service: str | None = None,
    ) -> EffectiveContainerEvidence:
        """Validate the effective client container and retain raw IDs privately."""
        container.reload()
        attrs = container.attrs
        if not isinstance(attrs, Mapping):
            raise ContainerAdmissionError("effective_container_inspection_unavailable")
        host = attrs.get("HostConfig")
        state = attrs.get("State")
        config = attrs.get("Config")
        mounts = attrs.get("Mounts")
        if (
            not isinstance(host, Mapping)
            or not isinstance(state, Mapping)
            or not isinstance(config, Mapping)
            or not isinstance(mounts, list)
        ):
            raise ContainerAdmissionError("effective_container_inspection_unavailable")
        if state.get("Running") is not True:
            raise ContainerAdmissionError("effective_container_not_running")
        privileged = host.get("Privileged")
        if not isinstance(privileged, bool):
            raise ContainerAdmissionError("effective_container_inspection_unavailable")
        if privileged:
            raise ContainerAdmissionError("effective_container_isolation_invalid")
        namespace_modes = {
            "CgroupnsMode": host.get("CgroupnsMode"),
            "IpcMode": host.get("IpcMode"),
            "PidMode": host.get("PidMode"),
            "UsernsMode": host.get("UsernsMode"),
            "UTSMode": host.get("UTSMode"),
        }
        if not all(
            value is None or isinstance(value, str)
            for value in namespace_modes.values()
        ):
            raise ContainerAdmissionError("effective_container_inspection_unavailable")
        if namespace_modes["PidMode"] not in (None, ""):
            raise ContainerAdmissionError("effective_container_isolation_invalid")
        if namespace_modes["IpcMode"] not in (None, "", "private"):
            raise ContainerAdmissionError("effective_container_isolation_invalid")
        if namespace_modes["CgroupnsMode"] not in (None, "", "private"):
            raise ContainerAdmissionError("effective_container_isolation_invalid")
        if namespace_modes["UsernsMode"] not in (None, ""):
            raise ContainerAdmissionError("effective_container_isolation_invalid")
        if namespace_modes["UTSMode"] not in (None, ""):
            raise ContainerAdmissionError("effective_container_isolation_invalid")
        network_mode = host.get("NetworkMode")
        if not isinstance(network_mode, str):
            raise ContainerAdmissionError("effective_container_inspection_unavailable")
        devices = host.get("Devices")
        if devices is None:
            # Docker Engine commonly serializes an empty device list as null.
            devices = []
        device_cgroup_rules = host.get("DeviceCgroupRules")
        device_requests = host.get("DeviceRequests")
        if (
            not isinstance(devices, list)
            or (
                device_cgroup_rules is not None
                and not isinstance(device_cgroup_rules, list)
            )
            or (device_requests is not None and not isinstance(device_requests, list))
        ):
            raise ContainerAdmissionError("effective_container_inspection_unavailable")
        if (
            network_mode.lower() == "host"
            or network_mode.lower().startswith("container:")
            or devices
            or device_cgroup_rules
            or device_requests
        ):
            raise ContainerAdmissionError("effective_container_isolation_invalid")
        cap_add = host.get("CapAdd")
        if cap_add is None:
            cap_add = []
        if not isinstance(cap_add, list) or not all(
            isinstance(item, str) and item for item in cap_add
        ):
            raise ContainerAdmissionError("effective_container_inspection_unavailable")
        if {item.upper().removeprefix("CAP_") for item in cap_add} - (
            _ALLOWED_TASK_CAPABILITIES
        ):
            raise ContainerAdmissionError("effective_container_isolation_invalid")
        security_opts = host.get("SecurityOpt")
        if security_opts is None:
            security_opts = []
        if not isinstance(security_opts, list) or not all(
            isinstance(item, str) for item in security_opts
        ):
            raise ContainerAdmissionError("effective_container_inspection_unavailable")
        unsafe_security_opts = {
            "apparmor:unconfined",
            "seccomp:unconfined",
            "label:disable",
        }
        if any(
            item.strip().lower().replace("=", ":", 1) in unsafe_security_opts
            for item in security_opts
        ):
            raise ContainerAdmissionError("effective_container_isolation_invalid")
        labels = config.get("Labels")
        if labels is None:
            labels = {}
        if not isinstance(labels, Mapping):
            raise ContainerAdmissionError("effective_container_inspection_unavailable")
        compose_project = labels.get("com.docker.compose.project")
        compose_service = labels.get("com.docker.compose.service")
        expected_task_volume = (
            f"{compose_project}_{_TASK_VOLUME_KEY}"
            if isinstance(compose_project, str) and compose_project
            else None
        )
        expected_bind_sources = getattr(container, _EXPECTED_BIND_SOURCES_ATTR, None)
        if external_command_isolation is None:
            external_command_isolation = bool(
                getattr(container, _EXTERNAL_COMMAND_ISOLATION_ATTR, False)
            )
        expected_compose_project = expected_compose_project or getattr(
            container, _EXPECTED_COMPOSE_PROJECT_ATTR, None
        )
        expected_compose_service = expected_compose_service or getattr(
            container, _EXPECTED_COMPOSE_SERVICE_ATTR, None
        )
        if external_command_isolation or expected_mounts is not None:
            network_settings = attrs.get("NetworkSettings")
            if not isinstance(network_settings, Mapping):
                raise ContainerAdmissionError(
                    "effective_container_network_inspection_unavailable"
                )
            if (
                not isinstance(expected_compose_project, str)
                or not expected_compose_project
                or not isinstance(expected_compose_service, str)
                or expected_compose_service not in {"client", "tester"}
                or compose_project != expected_compose_project
                or compose_service != expected_compose_service
            ):
                raise ContainerAdmissionError("effective_container_network_invalid")
            expected_network = f"{expected_compose_project}_default"
            live_networks = network_settings.get("Networks")
            if not isinstance(live_networks, Mapping):
                raise ContainerAdmissionError("effective_container_network_invalid")
            if network_mode == "none":
                if live_networks:
                    raise ContainerAdmissionError("effective_container_network_invalid")
            elif set(live_networks) != {expected_network}:
                raise ContainerAdmissionError("effective_container_network_invalid")
            else:
                attachment = live_networks.get(expected_network)
                if not isinstance(attachment, Mapping):
                    raise ContainerAdmissionError("effective_container_network_invalid")
                aliases = attachment.get("Aliases")
                if aliases is not None and (
                    not isinstance(aliases, list)
                    or not all(isinstance(alias, str) for alias in aliases)
                ):
                    raise ContainerAdmissionError(
                        "effective_container_inspection_unavailable"
                    )
                try:
                    network = container.client.networks.get(expected_network)
                    network.reload()
                    network_attrs = network.attrs
                except Exception as exc:
                    raise ContainerAdmissionError(
                        "effective_container_network_inspection_unavailable"
                    ) from exc
                network_labels = (
                    network_attrs.get("Labels")
                    if isinstance(network_attrs, Mapping)
                    else None
                )
                if not isinstance(network_labels, Mapping) or (
                    network_labels.get("com.docker.compose.project")
                    != expected_compose_project
                    or network_labels.get("com.docker.compose.network") != "default"
                ):
                    raise ContainerAdmissionError("effective_container_network_invalid")
                expected_network_id = attachment.get("NetworkID")
                actual_network_id = (
                    network_attrs.get("Id")
                    if isinstance(network_attrs, Mapping)
                    else None
                )
                if (
                    not isinstance(expected_network_id, str)
                    or not expected_network_id
                    or actual_network_id != expected_network_id
                ):
                    raise ContainerAdmissionError("effective_container_network_invalid")
                if network_mode not in ("default", expected_network):
                    raise ContainerAdmissionError("effective_container_network_invalid")
        if expected_mounts is not None and external_command_isolation:
            raise ContainerAdmissionError("effective_container_inspection_unavailable")
        if external_command_isolation and mounts:
            raise ContainerAdmissionError("effective_container_isolation_invalid")
        task_volume_source: str | None = None
        actual_mounts: list[tuple[str, str, str, bool]] = []
        for mount in mounts:
            if not isinstance(mount, Mapping):
                raise ContainerAdmissionError(
                    "effective_container_inspection_unavailable"
                )
            mount_type = mount.get("Type")
            source = mount.get("Source")
            destination = mount.get("Destination")
            if not all(
                isinstance(value, str) and value
                for value in (mount_type, source, destination)
            ):
                raise ContainerAdmissionError(
                    "effective_container_inspection_unavailable"
                )
            mount_type = mount_type.lower()
            writable = mount.get("RW")
            if expected_mounts is not None and not isinstance(writable, bool):
                raise ContainerAdmissionError(
                    "effective_container_inspection_unavailable"
                )
            if "docker.sock" in f"{source} {destination}".lower():
                raise ContainerAdmissionError("effective_container_isolation_invalid")
            if any(
                _SENSITIVE_MOUNT_PATH.search(path) for path in (source, destination)
            ):
                raise ContainerAdmissionError("effective_container_isolation_invalid")
            if any(
                path == root or path.startswith(f"{root}/")
                for path in (source.rstrip("/"), destination.rstrip("/"))
                for root in _RESTRICTED_MOUNT_ROOTS
            ):
                raise ContainerAdmissionError("effective_container_isolation_invalid")
            if mount_type == "bind":
                if expected_mounts is not None:
                    actual_mounts.append(
                        (
                            mount_type,
                            _canonical_bind_source(source),
                            destination,
                            writable,
                        )
                    )
                    continue
                if (
                    destination not in _ALLOWED_BIND_MOUNT_DESTINATIONS
                    or not isinstance(expected_bind_sources, Mapping)
                    or _canonical_bind_source(
                        str(expected_bind_sources.get(destination, ""))
                    )
                    != _canonical_bind_source(source)
                ):
                    raise ContainerAdmissionError(
                        "effective_container_isolation_invalid"
                    )
                continue
            if mount_type == "volume":
                volume_name = mount.get("Name")
                if expected_mounts is not None:
                    if not isinstance(volume_name, str) or not volume_name:
                        raise ContainerAdmissionError(
                            "effective_container_inspection_unavailable"
                        )
                    actual_mounts.append(
                        (mount_type, volume_name, destination, writable)
                    )
                    continue
                if (
                    destination not in _ALLOWED_TASK_VOLUME_DESTINATIONS
                    or not isinstance(volume_name, str)
                    or volume_name != expected_task_volume
                    or (task_volume_source is not None and source != task_volume_source)
                ):
                    raise ContainerAdmissionError(
                        "effective_container_isolation_invalid"
                    )
                task_volume_source = source
                continue
            raise ContainerAdmissionError("effective_container_isolation_invalid")
        if expected_mounts is not None and (
            len(actual_mounts) != len(set(actual_mounts))
            or set(actual_mounts) != expected_mounts
        ):
            raise ContainerAdmissionError("external_tester_mounts_invalid")
        env_entries = config.get("Env")
        if env_entries is None:
            env_entries = []
        if not isinstance(env_entries, list) or not all(
            isinstance(item, str)
            and "=" in item
            and _ENV_NAME.fullmatch(item.split("=", 1)[0])
            for item in env_entries
        ):
            raise ContainerAdmissionError("effective_container_inspection_unavailable")
        effective_env = [
            (item.split("=", 1)[0].upper(), item.split("=", 1)[1])
            for item in env_entries
        ]
        if external_command_isolation or expected_mounts is not None:
            if any(
                not _is_external_command_runtime_env(name, value)
                for name, value in effective_env
            ):
                raise ContainerAdmissionError("credential_isolation_unavailable")
            entrypoint = config.get("Entrypoint")
            command = config.get("Cmd")
            healthcheck = config.get("Healthcheck")
            if entrypoint not in (None, []) or command != _EXTERNAL_COMMAND_COMMAND:
                raise ContainerAdmissionError("effective_container_entrypoint_invalid")
            if not (
                isinstance(healthcheck, Mapping)
                and healthcheck.get("Test") == ["NONE"]
                and set(healthcheck) == {"Test"}
            ):
                raise ContainerAdmissionError("effective_container_healthcheck_invalid")
        else:
            if any(
                _is_credential_env_entry(name, value) for name, value in effective_env
            ):
                raise ContainerAdmissionError("credential_isolation_unavailable")
            if any(_is_controller_env_name(name) for name, _value in effective_env):
                raise ContainerAdmissionError("effective_container_isolation_invalid")
        container_id = attrs.get("Id") or getattr(container, "id", None)
        started_at = state.get("StartedAt")
        image_id = attrs.get("Image")
        if not all(
            isinstance(value, str) and value
            for value in (container_id, started_at, image_id)
        ):
            raise ContainerAdmissionError("effective_container_inspection_unavailable")
        return EffectiveContainerEvidence(
            container_id=container_id,
            started_at=started_at,
            image_id=image_id,
        )

    def _expected_external_tester_mounts(
        self, *, compose_project: str
    ) -> set[tuple[str, str, str, bool]]:
        if self._task_dir is None:
            raise ContainerAdmissionError(
                "external_tester_mount_inspection_unavailable"
            )
        return {
            (
                "volume",
                f"{compose_project}_{_TASK_VOLUME_KEY}",
                "/shared",
                True,
            ),
            (
                "bind",
                _canonical_bind_source(str(self._task_dir / "tests")),
                "/tests",
                False,
            ),
            (
                "bind",
                _canonical_bind_source(str(self._task_dir / "run-tests.sh")),
                "/task-assets/run-tests.sh",
                False,
            ),
            (
                "bind",
                _canonical_bind_source(str(self._task_dir)),
                "/task-assets/task",
                False,
            ),
        }

    def _inspect_external_tester(
        self, container: Container
    ) -> EffectiveContainerEvidence:
        """Validate a tester's runtime isolation and exact runner mounts."""
        container.reload()
        attrs = container.attrs
        config = attrs.get("Config") if isinstance(attrs, Mapping) else None
        labels = config.get("Labels") if isinstance(config, Mapping) else None
        compose_project = (
            labels.get("com.docker.compose.project")
            if isinstance(labels, Mapping)
            else None
        )
        if (
            not isinstance(compose_project, str)
            or compose_project != self._client_container_name.lower()
        ):
            raise ContainerAdmissionError(
                "external_tester_mount_inspection_unavailable"
            )
        evidence = self.inspect_effective_container(
            container,
            external_command_isolation=False,
            expected_mounts=self._expected_external_tester_mounts(
                compose_project=compose_project
            ),
            expected_compose_project=compose_project,
            expected_compose_service="tester",
        )
        if (
            self._admitted_client is None
            or evidence.image_id != self._admitted_client.image_id
        ):
            raise ContainerAdmissionError("external_tester_image_invalid")
        return evidence

    @staticmethod
    def _evidence_matches(
        actual: EffectiveContainerEvidence, expected: EffectiveContainerEvidence
    ) -> bool:
        return actual == expected

    def _validate_live_external_container(
        self,
        *,
        configured_name: str | None,
        admitted: EffectiveContainerEvidence | None,
        tester: bool,
    ) -> EffectiveContainerEvidence:
        if not self._external_command_isolation:
            raise ContainerAdmissionError(
                "live_container_validation_requires_external_command_isolation"
            )
        if configured_name is None or admitted is None:
            raise ContainerAdmissionError("live_container_admission_missing")
        not_found = _get_docker_module().errors.NotFound
        try:
            by_id = self._client.containers.get(admitted.container_id)
            by_name = self._client.containers.get(configured_name)
        except not_found as exc:
            raise ContainerAdmissionError("live_container_generation_missing") from exc
        except Exception as exc:
            raise ContainerAdmissionError(
                "live_container_inspection_unavailable"
            ) from exc

        try:
            id_evidence = (
                self._inspect_external_tester(by_id)
                if tester
                else self.inspect_effective_container(
                    by_id,
                    external_command_isolation=True,
                    expected_compose_project=self._client_container_name.lower(),
                    expected_compose_service="client",
                )
            )
            name_evidence = (
                self._inspect_external_tester(by_name)
                if tester
                else self.inspect_effective_container(
                    by_name,
                    external_command_isolation=True,
                    expected_compose_project=self._client_container_name.lower(),
                    expected_compose_service="client",
                )
            )
        except ContainerAdmissionError:
            raise
        except Exception as exc:
            raise ContainerAdmissionError(
                "live_container_inspection_unavailable"
            ) from exc
        if not (
            self._evidence_matches(id_evidence, admitted)
            and self._evidence_matches(name_evidence, admitted)
        ):
            raise ContainerAdmissionError("live_container_generation_mismatch")
        return id_evidence

    def validate_live_client(self) -> EffectiveContainerEvidence:
        """Revalidate the admitted external solver by immutable ID and name."""
        return self._validate_live_external_container(
            configured_name=self._client_container_name,
            admitted=self._admitted_client,
            tester=False,
        )

    def validate_live_tester(self) -> EffectiveContainerEvidence:
        """Revalidate the admitted external tester and its exact live mounts."""
        return self._validate_live_external_container(
            configured_name=self._tester_container_name,
            admitted=self._admitted_tester,
            tester=True,
        )

    @property
    def tester_container(self) -> Container | None:
        return self._tester_container

    @property
    def image_resolution(self) -> DockerImageResolution:
        return self._image_resolution

    def _release_external_command_override(self) -> None:
        if self._external_command_override_dir is None:
            return
        override_path = Path(self._external_command_override_dir.name) / (
            _EXTERNAL_COMMAND_OVERRIDE_FILENAME
        )
        self._compose_files = [
            path for path in self._compose_files if path != override_path
        ]
        self._external_command_override_dir.cleanup()
        self._external_command_override_dir = None

    def _remove_external_container(
        self,
        container_name: str,
        *,
        stop_first: bool,
        expected_container_id: str | None = None,
    ) -> None:
        """Remove one ID-bound generation and prove its ID and name absent."""
        not_found = _get_docker_module().errors.NotFound
        lookup_key = expected_container_id or container_name
        try:
            container = self._client.containers.get(lookup_key)
        except not_found:
            container = None
        except Exception as exc:
            raise ContainerAdmissionError(
                f"external_container_teardown_unproven: {container_name}"
            ) from exc
        if container is not None:
            actual_container_id = (
                container.attrs.get("Id")
                if isinstance(getattr(container, "attrs", None), Mapping)
                else None
            ) or getattr(container, "id", None)
            if (
                expected_container_id is not None
                and actual_container_id != expected_container_id
            ):
                raise ContainerAdmissionError(
                    "external_client_destroy_container_id_mismatch"
                )

        if stop_first and container is not None:
            try:
                container.stop(timeout=10)
            except not_found:
                container = None
            except Exception as exc:
                self._logger.warning(
                    "External container %s did not stop cleanly; "
                    "forcing termination: %s",
                    container_name,
                    exc,
                )
        if container is not None:
            try:
                container.kill()
            except not_found:
                container = None
            except Exception as exc:
                # `stop` may already have transitioned the container to a
                # non-running state.  Still issue kill after a failed Compose
                # down, then rely on forced removal and the final lookup.
                self._logger.warning(
                    "External container %s did not kill cleanly: %s",
                    container_name,
                    exc,
                )

        if container is not None:
            try:
                container.remove(force=True)
            except not_found:
                pass
            except Exception as exc:
                # Do not infer failure or success from one SDK operation: the
                # final lookup is the teardown proof and handles daemon races.
                self._logger.warning(
                    "External container %s did not remove cleanly: %s",
                    container_name,
                    exc,
                )

        if expected_container_id is not None:
            try:
                self._client.containers.get(expected_container_id)
            except not_found:
                pass
            except Exception as exc:
                raise ContainerAdmissionError(
                    f"external_container_teardown_unproven: {container_name}"
                ) from exc
            else:
                raise ContainerAdmissionError(
                    f"external_container_teardown_unproven: {container_name}"
                )

        try:
            occupant = self._client.containers.get(container_name)
        except not_found:
            return
        except Exception as exc:
            raise ContainerAdmissionError(
                f"external_container_teardown_unproven: {container_name}"
            ) from exc
        occupant_id = (
            occupant.attrs.get("Id")
            if isinstance(getattr(occupant, "attrs", None), Mapping)
            else None
        ) or getattr(occupant, "id", None)
        if expected_container_id is not None and occupant_id != expected_container_id:
            error = (
                "external_client_destroy_name_replaced"
                if container_name == self._client_container_name
                else f"external_container_generation_replaced: {container_name}"
            )
            raise ContainerAdmissionError(error)
        raise ContainerAdmissionError(
            f"external_container_teardown_unproven: {container_name}"
        )

    def destroy_external_client(self, *, expected_container_id: str) -> None:
        """Force-remove the external solver while leaving its tester alive.

        The harness calls this after capturing runner-owned evidence and before
        accepting the result or starting verifier work.  Binding the removal to
        the admitted container ID prevents a same-name replacement from being
        mistaken for the solver generation whose evidence was captured.
        """
        if not self._external_command_isolation:
            raise ContainerAdmissionError(
                "external_client_destroy_requires_external_command_isolation"
            )
        if not isinstance(expected_container_id, str) or not expected_container_id:
            raise ContainerAdmissionError(
                "external_client_destroy_container_id_invalid"
            )
        if self._admitted_client is None:
            raise ContainerAdmissionError("external_client_destroy_admission_missing")
        if self._admitted_client.container_id != expected_container_id:
            raise ContainerAdmissionError(
                "external_client_destroy_container_id_mismatch"
            )
        cached_container = self._client_container
        cached_container_id = (
            cached_container.attrs.get("Id")
            if cached_container is not None
            and isinstance(getattr(cached_container, "attrs", None), Mapping)
            else None
        ) or getattr(cached_container, "id", None)
        if (
            cached_container is not None
            and cached_container_id != expected_container_id
        ):
            raise ContainerAdmissionError(
                "external_client_destroy_container_id_mismatch"
            )
        self._remove_external_container(
            self._client_container_name,
            stop_first=False,
            expected_container_id=expected_container_id,
        )
        self._client_container = None
        self._cleanup_client_container_id = None

    def _remove_external_containers(self) -> None:
        failures: list[tuple[str, ContainerAdmissionError]] = []
        containers = dict.fromkeys(
            (name, attribute, cleanup_attribute, cleanup_id)
            for name, attribute, cleanup_attribute, cleanup_id in (
                (
                    self._client_container_name,
                    "_client_container",
                    "_cleanup_client_container_id",
                    self._cleanup_client_container_id,
                ),
                (
                    self._tester_container_name,
                    "_tester_container",
                    "_cleanup_tester_container_id",
                    self._cleanup_tester_container_id,
                ),
            )
            if name
        )
        not_found = _get_docker_module().errors.NotFound
        for container_name, attribute, cleanup_attribute, cleanup_id in containers:
            try:
                if cleanup_id is None:
                    try:
                        self._client.containers.get(container_name)
                    except not_found:
                        pass
                    except Exception as exc:
                        raise ContainerAdmissionError(
                            f"external_container_teardown_unproven: {container_name}"
                        ) from exc
                    else:
                        raise ContainerAdmissionError(
                            f"external_container_cleanup_identity_missing: {container_name}"
                        )
                else:
                    self._remove_external_container(
                        container_name,
                        stop_first=True,
                        expected_container_id=cleanup_id,
                    )
            except ContainerAdmissionError as exc:
                failures.append((container_name, exc))
            else:
                setattr(self, attribute, None)
                setattr(self, cleanup_attribute, None)
        if failures:
            if len(failures) == 1:
                raise failures[0][1]
            for _name, failure in failures:
                if "generation_replaced" in str(failure) or (
                    "destroy_name_replaced" in str(failure)
                ):
                    raise failure
            names = ", ".join(name for name, _exc in failures)
            raise ContainerAdmissionError(
                f"external_container_teardown_unproven: {names}"
            ) from failures[0][1]

    def _remove_external_default_network(self) -> None:
        expected_network_id = self._cleanup_default_network_id
        if expected_network_id is None:
            return
        network_name = f"{self._client_container_name.lower()}_default"
        not_found = _get_docker_module().errors.NotFound
        try:
            network = self._client.networks.get(expected_network_id)
            network.reload()
            attrs = network.attrs
        except not_found:
            network = None
        except Exception as exc:
            raise ContainerAdmissionError("external_network_teardown_unproven") from exc
        if network is not None:
            labels = attrs.get("Labels") if isinstance(attrs, Mapping) else None
            actual_id = attrs.get("Id") if isinstance(attrs, Mapping) else None
            if (
                actual_id != expected_network_id
                or not isinstance(labels, Mapping)
                or labels.get("com.docker.compose.project")
                != self._client_container_name.lower()
                or labels.get("com.docker.compose.network") != "default"
            ):
                raise ContainerAdmissionError("external_network_teardown_unproven")
            try:
                network.remove()
            except not_found:
                pass
            except Exception as exc:
                raise ContainerAdmissionError(
                    "external_network_teardown_unproven"
                ) from exc
        try:
            occupant = self._client.networks.get(network_name)
            occupant.reload()
            occupant_attrs = occupant.attrs
        except not_found:
            self._cleanup_default_network_id = None
            return
        except Exception as exc:
            raise ContainerAdmissionError("external_network_teardown_unproven") from exc
        occupant_id = (
            occupant_attrs.get("Id") if isinstance(occupant_attrs, Mapping) else None
        )
        if occupant_id != expected_network_id:
            raise ContainerAdmissionError("external_network_generation_replaced")
        raise ContainerAdmissionError("external_network_teardown_unproven")

    def stop(self) -> None:
        if self._external_command_isolation:
            try:
                # Never run project-wide `compose down`: it resolves by mutable
                # project/name labels and could remove a replacement generation.
                # The immutable IDs captured during `up` are the sole container
                # teardown authority for external-command trials.
                self._remove_external_containers()
                self._remove_external_default_network()
            except ContainerAdmissionError:
                # Preserve the exact override for an identical, inspectable
                # retry when teardown cannot be proven.
                raise
            self._release_external_command_override()
            return
        try:
            self._run_compose(["down"])
            if self._cleanup:
                self._run_compose(["down", "--rmi", "all", "--volumes"])
        except Exception as exc:
            self._logger.error(f"Error stopping compose services: {exc}")
            return
        else:
            self._release_external_command_override()

    def build(self) -> None:
        self._run_compose(["build"])

    # -- file copy --

    @staticmethod
    def _create_tar(paths: list[Path], container_filename: str | None) -> io.BytesIO:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for p in paths:
                if p.is_file():
                    arcname = container_filename if container_filename else p.name
                    tar.add(p, arcname=arcname)
                elif p.is_dir():
                    # Preserve directory layout under ``arcname`` (e.g. ``gold_patches/*.diff``),
                    # not only basenames (which would flatten and break progressive ``solution.sh``).
                    tar.add(p, arcname=p.name, recursive=True)
                else:
                    raise ValueError(f"Path {p} is neither file nor directory")
        buf.seek(0)
        return buf

    @staticmethod
    def copy_to_container(
        container: Container,
        paths: list[Path] | Path,
        container_dir: str | None = None,
        container_filename: str | None = None,
    ) -> None:
        """Copy files / directories into a running container."""
        container_dir = container_dir or (
            container.attrs.get("Config", {}).get("WorkingDir")
            if container.attrs
            else None
        )
        if container_dir is None:
            raise ValueError("Container working directory not found")

        if isinstance(paths, Path):
            paths = [paths]

        container.exec_run(f"mkdir -p {container_dir}")
        buf = DockerComposeManager._create_tar(paths, container_filename)
        container.put_archive(container_dir, buf.read())
        buf.close()

    def copy_to_client_container(
        self,
        paths: list[Path] | Path,
        container_dir: str | None = None,
        container_filename: str | None = None,
    ) -> None:
        if self._client_container is None:
            raise ValueError("Client container not started")
        self.copy_to_container(
            container=self._client_container,
            paths=paths,
            container_dir=container_dir,
            container_filename=container_filename,
        )


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


@contextmanager
def spin_up_container(
    client_container_name: str,
    client_image_name: str,
    docker_compose_path: Path,
    docker_name_prefix: str | None = None,
    docker_image_strategy: DockerImageStrategy | None = None,
    no_rebuild: bool = False,
    cleanup: bool = False,
    logs_path: Path | None = None,
    agent_logs_path: Path | None = None,
) -> Generator[Container, None, None]:
    """Spin up a docker-compose stack, yield the client container, then tear down."""
    mgr = DockerComposeManager(
        client_container_name=client_container_name,
        client_image_name=client_image_name,
        docker_compose_path=docker_compose_path,
        docker_image_name_prefix=docker_name_prefix,
        docker_image_strategy=docker_image_strategy,
        no_rebuild=no_rebuild,
        cleanup=cleanup,
        logs_path=logs_path,
        agent_logs_path=agent_logs_path,
    )
    try:
        container = mgr.start()
        yield container
    finally:
        mgr.stop()
