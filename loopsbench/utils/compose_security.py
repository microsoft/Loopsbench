"""Security validation for task-owned Docker Compose files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ComposeSecurityIssue:
    code: str
    message: str
    path: Path


ALLOWED_LOOPSBENCH_BIND_SOURCES = {
    "$LOOPSBENCH_TASK_AGENT_LOGS_PATH",
    "$LOOPSBENCH_TASK_LOGS_PATH",
    "${LOOPSBENCH_TASK_AGENT_LOGS_PATH}",
    "${LOOPSBENCH_TASK_LOGS_PATH}",
}

DANGEROUS_CAPABILITIES = {
    "ALL",
    "AUDIT_CONTROL",
    "BPF",
    "CHECKPOINT_RESTORE",
    "DAC_OVERRIDE",
    "DAC_READ_SEARCH",
    "IPC_LOCK",
    "IPC_OWNER",
    "LINUX_IMMUTABLE",
    "MAC_ADMIN",
    "MAC_OVERRIDE",
    "MKNOD",
    "PERFMON",
    "SYS_ADMIN",
    "SYS_BOOT",
    "SYS_MODULE",
    "SYS_NICE",
    "SYS_PACCT",
    "SYS_PTRACE",
    "SYS_RAWIO",
    "SYS_RESOURCE",
    "SYS_TIME",
    "SYS_TTY_CONFIG",
    "SYSLOG",
    "WAKE_ALARM",
}


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _as_sequence(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _normalize_capability(value: Any) -> str:
    cap = str(value).strip().upper()
    return cap.removeprefix("CAP_")


def _is_docker_socket_path(value: Any) -> bool:
    return "docker.sock" in str(value)


def _split_short_volume(value: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    brace_depth = 0
    index = 0
    while index < len(value):
        char = value[index]
        if char == "$" and index + 1 < len(value) and value[index + 1] == "{":
            brace_depth += 1
            current.append(char)
            index += 1
            current.append(value[index])
        elif char == "}" and brace_depth:
            brace_depth -= 1
            current.append(char)
        elif char == ":" and brace_depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    parts.append("".join(current))
    return parts


def _looks_like_env_reference(value: str) -> bool:
    return value.startswith("$")


def _looks_like_host_path(source: str) -> bool:
    source = source.strip()
    if not source or source in ALLOWED_LOOPSBENCH_BIND_SOURCES:
        return False
    if _looks_like_env_reference(source):
        return True
    if source in {".", ".."}:
        return True
    if source.startswith(("/", "./", "../", "~")):
        return True
    return "/" in source or "\\" in source


def _add_issue(
    issues: list[ComposeSecurityIssue],
    *,
    code: str,
    message: str,
    path: Path,
) -> None:
    issues.append(ComposeSecurityIssue(code=code, message=message, path=path))


def _validate_build_config(
    *,
    service_name: str,
    build_config: Any,
    compose_path: Path,
    issues: list[ComposeSecurityIssue],
) -> None:
    contexts: list[Any] = []
    dockerfiles: list[Any] = []
    if isinstance(build_config, str):
        contexts.append(build_config)
    elif isinstance(build_config, dict):
        contexts.append(build_config.get("context", "."))
        dockerfiles.append(build_config.get("dockerfile"))
        additional_contexts = build_config.get("additional_contexts")
        if isinstance(additional_contexts, dict):
            contexts.extend(additional_contexts.values())
        elif isinstance(additional_contexts, list):
            for item in additional_contexts:
                if isinstance(item, str) and "=" in item:
                    contexts.append(item.split("=", 1)[1])
                else:
                    contexts.append(item)

    for value in [item for item in contexts + dockerfiles if item is not None]:
        raw_path = str(value).strip()
        if not raw_path:
            continue
        if "://" in raw_path:
            continue
        path = Path(raw_path)
        if path.is_absolute() or _looks_like_env_reference(raw_path):
            _add_issue(
                issues,
                code="compose_host_build_path",
                message=(
                    f"Service `{service_name}` build path `{raw_path}` must be a "
                    "relative path inside the task directory."
                ),
                path=compose_path,
            )
            continue
        resolved = (compose_path.parent / path).resolve()
        try:
            resolved.relative_to(compose_path.parent.resolve())
        except ValueError:
            _add_issue(
                issues,
                code="compose_host_build_path",
                message=(
                    f"Service `{service_name}` build path `{raw_path}` must stay "
                    "inside the task directory."
                ),
                path=compose_path,
            )


def _validate_service_volumes(
    *,
    service_name: str,
    volumes: Any,
    compose_path: Path,
    issues: list[ComposeSecurityIssue],
) -> None:
    for volume in _as_sequence(volumes):
        if isinstance(volume, str):
            parts = _split_short_volume(volume)
            if any(_is_docker_socket_path(part) for part in parts):
                _add_issue(
                    issues,
                    code="compose_docker_socket_mount",
                    message=(
                        f"Service `{service_name}` must not mount the Docker socket."
                    ),
                    path=compose_path,
                )
            if len(parts) >= 2 and _looks_like_host_path(parts[0]):
                _add_issue(
                    issues,
                    code="compose_host_bind_mount",
                    message=(
                        f"Service `{service_name}` volume `{volume}` mounts a host "
                        "path. Task Compose files may only use named volumes and "
                        "LoopsBench-managed log mounts."
                    ),
                    path=compose_path,
                )
            continue

        if not isinstance(volume, dict):
            continue

        source = volume.get("source") or volume.get("src")
        target = volume.get("target") or volume.get("dst") or volume.get("destination")
        volume_type = str(volume.get("type") or "").strip().lower()
        if any(_is_docker_socket_path(value) for value in (source, target)):
            _add_issue(
                issues,
                code="compose_docker_socket_mount",
                message=f"Service `{service_name}` must not mount the Docker socket.",
                path=compose_path,
            )
        if volume_type == "bind" or (
            isinstance(source, str) and _looks_like_host_path(source)
        ):
            _add_issue(
                issues,
                code="compose_host_bind_mount",
                message=(
                    f"Service `{service_name}` declares a host bind mount. Task "
                    "Compose files may only use named volumes and "
                    "LoopsBench-managed log mounts."
                ),
                path=compose_path,
            )


def _validate_service(
    *,
    service_name: str,
    service_config: dict[str, Any],
    compose_path: Path,
    issues: list[ComposeSecurityIssue],
) -> None:
    if _is_truthy(service_config.get("privileged")):
        _add_issue(
            issues,
            code="compose_privileged",
            message=f"Service `{service_name}` must not set `privileged: true`.",
            path=compose_path,
        )

    if service_config.get("pid") not in (None, ""):
        _add_issue(
            issues,
            code="compose_pid_namespace",
            message=f"Service `{service_name}` must not override `pid` namespace.",
            path=compose_path,
        )

    for field_name in ("ipc", "cgroup", "userns_mode", "uts"):
        value = service_config.get(field_name)
        if isinstance(value, str) and value.strip().lower() == "host":
            _add_issue(
                issues,
                code=f"compose_{field_name}_host",
                message=f"Service `{service_name}` must not set `{field_name}: host`.",
                path=compose_path,
            )

    network_mode = service_config.get("network_mode")
    if isinstance(network_mode, str) and network_mode.strip().lower() == "host":
        _add_issue(
            issues,
            code="compose_network_mode_host",
            message=f"Service `{service_name}` must not set `network_mode: host`.",
            path=compose_path,
        )

    for field_name in ("devices", "device_cgroup_rules", "volumes_from"):
        if _as_sequence(service_config.get(field_name)):
            _add_issue(
                issues,
                code=f"compose_{field_name}",
                message=f"Service `{service_name}` must not set `{field_name}`.",
                path=compose_path,
            )

    dangerous_caps = sorted(
        {
            _normalize_capability(capability)
            for capability in _as_sequence(service_config.get("cap_add"))
        }
        & DANGEROUS_CAPABILITIES
    )
    for capability in dangerous_caps:
        _add_issue(
            issues,
            code="compose_dangerous_capability",
            message=(
                f"Service `{service_name}` must not add dangerous capability "
                f"`{capability}`."
            ),
            path=compose_path,
        )

    security_opts = [
        str(option).strip().lower()
        for option in _as_sequence(service_config.get("security_opt"))
    ]
    for option in security_opts:
        if option in {"apparmor:unconfined", "seccomp:unconfined", "label:disable"}:
            _add_issue(
                issues,
                code="compose_unsafe_security_opt",
                message=(
                    f"Service `{service_name}` must not disable container security "
                    f"with `security_opt: {option}`."
                ),
                path=compose_path,
            )

    if _is_truthy(service_config.get("use_api_socket")):
        _add_issue(
            issues,
            code="compose_docker_socket_mount",
            message=f"Service `{service_name}` must not request the Docker API socket.",
            path=compose_path,
        )

    _validate_service_volumes(
        service_name=service_name,
        volumes=service_config.get("volumes"),
        compose_path=compose_path,
        issues=issues,
    )
    if "build" in service_config:
        _validate_build_config(
            service_name=service_name,
            build_config=service_config.get("build"),
            compose_path=compose_path,
            issues=issues,
        )


def validate_compose_security(compose_path: Path) -> list[ComposeSecurityIssue]:
    """Return security issues in a task-owned Docker Compose file."""
    issues: list[ComposeSecurityIssue] = []
    try:
        payload = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        return [
            ComposeSecurityIssue(
                code="invalid_compose_yaml",
                message=f"docker compose file is not valid YAML: {exc}",
                path=compose_path,
            )
        ]

    if not isinstance(payload, dict):
        return [
            ComposeSecurityIssue(
                code="invalid_compose_yaml",
                message="docker compose file must contain a YAML mapping.",
                path=compose_path,
            )
        ]

    if payload.get("include"):
        _add_issue(
            issues,
            code="compose_include_not_allowed",
            message="Task Compose files must not use top-level `include`.",
            path=compose_path,
        )

    services = payload.get("services")
    if not isinstance(services, dict):
        _add_issue(
            issues,
            code="invalid_compose_services",
            message="docker compose file must define a `services` mapping.",
            path=compose_path,
        )
        return issues

    top_level_volumes = payload.get("volumes")
    if isinstance(top_level_volumes, dict):
        for volume_name, volume_config in top_level_volumes.items():
            if isinstance(volume_config, dict) and volume_config.get("driver_opts"):
                _add_issue(
                    issues,
                    code="compose_volume_driver_opts",
                    message=(
                        f"Top-level volume `{volume_name}` must not set `driver_opts`."
                    ),
                    path=compose_path,
                )

    for service_name, service_config in services.items():
        if not isinstance(service_config, dict):
            continue
        if service_config.get("extends"):
            _add_issue(
                issues,
                code="compose_extends_not_allowed",
                message=f"Service `{service_name}` must not use `extends`.",
                path=compose_path,
            )
        _validate_service(
            service_name=str(service_name),
            service_config=service_config,
            compose_path=compose_path,
            issues=issues,
        )

    return issues


def format_compose_security_issues(issues: list[ComposeSecurityIssue]) -> str:
    return "\n".join(f"- {issue.message}" for issue in issues)
