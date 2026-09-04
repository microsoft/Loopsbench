"""Provider-neutral external command agent.

The command must already be available inside the task container. LoopsBench
passes the task instruction through a small JSON request and continues to own
the task container, verifier, and official score.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shlex
import tarfile
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from loopsbench.agents.base_agent import AgentResult, AgentSafetyError, BaseAgent
from loopsbench.harness.models import FailureMode
from loopsbench.terminal.docker_compose_manager import (
    ContainerAdmissionError,
    DockerComposeManager,
    EffectiveContainerEvidence,
)
from loopsbench.terminal.tmux_session import TmuxSession

EXTERNAL_AGENT_REQUEST_SCHEMA_VERSION = "external_agent_request_v2"
EXTERNAL_AGENT_RESULT_SCHEMA_VERSION = "external_agent_result_v2"
BENCHMARK_LAUNCH_ADMISSION_RECEIPT_SCHEMA_VERSION = (
    "benchmark_launch_admission_receipt_v0"
)
EXTERNAL_AGENT_CONTAINMENT_SCHEMA_VERSION = "external_agent_containment_v1"
EXTERNAL_AGENT_CONTAINMENT_VERIFICATION_SCHEMA_VERSION = (
    "external_agent_containment_verification_v1"
)
_CONTAINER_REQUEST_PATH = "/tmp/loopsbench-external-command/request.json"
_CONTAINER_RESULT_PATH = "/tmp/loopsbench-external-command/result.json"
_CONTAINER_LAUNCH_PATH = "/tmp/loopsbench-external-command/launch.sh"
_CONTAINER_LAUNCH_DONE_PATH = "/tmp/loopsbench-external-command/launch.done"
_PUBLIC_LAUNCH_COMMAND = f"/bin/sh {_CONTAINER_LAUNCH_PATH}"
_CONTAINMENT_POSTCONDITION = "destroyed_before_result_consumption"
_CONTAINMENT_PROOF_SCHEMA_VERSION = "loopsbench_external_agent_containment_proof_v1"
_RECEIPT_SCHEMA_VERSION = "external_agent_phase_receipt_v2"
_RECEIPT_KEYS = {
    "schema_version",
    "classification",
    "command_recorded",
    "command_argument_count",
    "duration_ms",
    "instruction_recorded",
    "workspace_recorded",
    "instruction_sha256",
    "instruction_chars",
    "launch_binding_digest",
}
_MAX_RESULT_ARCHIVE_BYTES = 1 * 1024 * 1024
_MAX_RESULT_JSON_BYTES = 256 * 1024
_MAX_RESULT_ARCHIVE_MEMBERS = 4
_MAX_WORKSPACE_ARCHIVE_BYTES = 256 * 1024 * 1024
_MAX_WORKSPACE_EXPANDED_BYTES = 1 * 1024 * 1024 * 1024
_MAX_WORKSPACE_ARCHIVE_MEMBERS = 100_000
_MAX_WORKSPACE_MEMBER_BYTES = 128 * 1024 * 1024
_PUBLIC_BOUNDARY = {
    "raw_instruction_recorded": False,
    "raw_credential_recorded": False,
    "raw_controller_state_recorded": False,
    "raw_runtime_identity_recorded": False,
    "path_recorded": False,
}
_PUBLIC_TOKEN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:@+-"
)
_PUBLIC_TOKEN_START_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
)
_PUBLIC_COMPACT_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,127}$")
_PUBLIC_NAMESPACED_TOKEN_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,79}/[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,119}$"
)
_PUBLIC_IDENTITY_DIGEST_PATTERN = re.compile(
    r"^public:(?P<kind>benchmark|case|run|arm|authority):sha256:(?P<digest>[0-9a-f]{64})$"
)
_CREDENTIAL_NAME_PATTERN = re.compile(
    r"(?:^|[._-])(?:"
    r"api[._-]?key|access[._-]?key(?:[._-]?id)?|"
    r"client[._-]?secret|secret(?:[._-]?key)?|"
    r"access[._-]?token|refresh[._-]?token|token|"
    r"password|passwd|credential(?:s)?|authorization"
    r")$",
    re.IGNORECASE,
)
_AUTHORIZATION_HEADER_PATTERN = re.compile(
    r"(?:^|[=,;{\s\"'])authorization\s*:", re.IGNORECASE
)
_URL_USERINFO_PATTERN = re.compile(
    r"(?:^|=)[a-z][a-z0-9+.-]*://[^/?#\s]+@", re.IGNORECASE
)
_CREDENTIAL_HEADER_PATTERN = re.compile(
    r"(?:^|[=,;{\s\"'])(?:authorization|proxy-authorization|"
    r"x-api-key|api-key|access-token)\s*[:=]",
    re.IGNORECASE,
)
_CREDENTIAL_FLAG_SUFFIX_PATTERN = re.compile(
    r"(?:^|[._-])(?:"
    r"api[._-]?key|access[._-]?key(?:[._-]?id)?|"
    r"client[._-]?secret|secret(?:[._-]?key)?|"
    r"access[._-]?token|refresh[._-]?token|bearer[._-]?token|token|"
    r"password|passwd|credential(?:s)?|authorization"
    r")(?:[._-](?:file|path|value))?$",
    re.IGNORECASE,
)
_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    r"(?:^|[\s,;{\"'])(?:"
    r"api[._-]?key|access[._-]?key(?:[._-]?id)?|"
    r"client[._-]?secret|secret(?:[._-]?key)?|"
    r"access[._-]?token|refresh[._-]?token|bearer[._-]?token|"
    r"password|passwd|credential(?:s)?|authorization"
    r")\s*=",
    re.IGNORECASE,
)
_CURL_CREDENTIAL_VALUE_FLAGS = frozenset(
    {
        "-b",
        "-K",
        "-u",
        "--cert",
        "--config",
        "--cookie",
        "--ftp-account",
        "--key",
        "--mail-auth",
        "--netrc-file",
        "--oauth2-bearer",
        "--pass",
        "--proxy-cert",
        "--proxy-key",
        "--proxy-pass",
        "--proxy-tlspassword",
        "--proxy-tlsuser",
        "--proxy-user",
        "--tlspassword",
        "--tlsuser",
        "--user",
    }
)
_CURL_CREDENTIAL_SOURCE_FLAGS = frozenset({"-n", "--netrc", "--netrc-optional"})
_CURL_ATTACHED_CREDENTIAL_FLAGS = ("-b", "-K", "-u")
_CURL_HEADER_VALUE_FLAGS = frozenset({"-H", "--header", "--proxy-header"})
_SENSITIVE_HEADER_NAMES = frozenset(
    {
        "api-key",
        "access-token",
        "authorization",
        "cookie",
        "proxy-authorization",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
    }
)
_NESTED_COMMAND_JSON_FLAGS = frozenset({"--solver-command-json"})
_POSIX_SHELL_INTERPRETERS = frozenset({"ash", "bash", "dash", "ksh", "sh", "zsh"})
_OPAQUE_JWT_PATTERN = re.compile(
    r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$"
)
_OPAQUE_SINGLE_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_OPAQUE_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_OPAQUE_SEGMENT_SPLIT_PATTERN = re.compile(r"[-_:.@/]")
_MAX_CREDENTIAL_SCAN_DEPTH = 4
_MAX_CREDENTIAL_SCAN_NODES = 256
_MAX_NESTED_COMMAND_JSON_CHARS = 64 * 1024
_PUBLIC_IDENTITY_FIELD_KINDS = {
    "benchmark_id": "benchmark",
    "case_id": "case",
    "run_id": "run",
    "arm_id": "arm",
    "provider_authority": "authority",
}
_PUBLIC_ROUTE_LABEL_FIELDS = frozenset({"expected_provider", "expected_model"})


@dataclass
class _CredentialScanBudget:
    remaining_nodes: int = _MAX_CREDENTIAL_SCAN_NODES

    def consume(self) -> bool:
        if self.remaining_nodes <= 0:
            return False
        self.remaining_nodes -= 1
        return True


@dataclass(frozen=True)
class ExternalCommandSnapshot:
    """Process-private state captured from one paused solver generation."""

    container_id: str
    post_agent_pane: str
    workspace_archive: bytes | None
    raw_result_archive: bytes | None
    timed_out: bool
    execution_error: Exception | None
    snapshot_error: AgentSafetyError | None
    instruction: str
    containment_receipt_ref: str
    launch_binding_digest: str
    launch_admission: Mapping[str, object]
    logging_dir: Path
    command_argument_count: int
    duration_ms: int


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_digest(value: object) -> str:
    return _sha256(_canonical_json(value))


def _public_identity_digest(value: str, *, kind: str) -> str:
    digest = _sha256(f"loopsbench-public-identity-v1\0{kind}\0{value}")
    return f"public:{kind}:sha256:{digest}"


def _public_token(
    value: object,
    field: str,
    *,
    allow_namespaced: bool = False,
    allow_public_digest_kind: str | None = None,
) -> str:
    if not isinstance(value, str):
        raise AgentSafetyError(f"launch_binding_{field}_invalid")
    token = value.strip()
    if not token:
        raise AgentSafetyError(f"launch_binding_{field}_invalid")
    if token.startswith("public:"):
        match = _PUBLIC_IDENTITY_DIGEST_PATTERN.fullmatch(token)
        if (
            allow_public_digest_kind is None
            or match is None
            or match.group("kind") != allow_public_digest_kind
        ):
            raise AgentSafetyError(f"launch_binding_{field}_invalid")
        return token
    if _PUBLIC_COMPACT_TOKEN_PATTERN.fullmatch(token):
        return token
    if allow_namespaced and _PUBLIC_NAMESPACED_TOKEN_PATTERN.fullmatch(token):
        return token
    raise AgentSafetyError(f"launch_binding_{field}_invalid")


def _credential_name(value: str) -> bool:
    candidate = value.strip().lstrip("-").split("=", 1)[0]
    return bool(
        _CREDENTIAL_NAME_PATTERN.search(candidate)
        or _CREDENTIAL_FLAG_SUFFIX_PATTERN.search(candidate)
    )


def _looks_like_opaque_public_token(value: str) -> bool:
    if _OPAQUE_JWT_PATTERN.fullmatch(value):
        return True
    if _OPAQUE_SINGLE_SEGMENT_PATTERN.fullmatch(value):
        return True
    segments = [
        segment for segment in _OPAQUE_SEGMENT_SPLIT_PATTERN.split(value) if segment
    ]
    return any(_OPAQUE_SEGMENT_PATTERN.fullmatch(segment) for segment in segments)


def _public_identity_token(value: object, field: str) -> str:
    kind = _PUBLIC_IDENTITY_FIELD_KINDS[field]
    token = _public_token(
        value,
        field,
        allow_namespaced=(field == "case_id"),
        allow_public_digest_kind=kind,
    )
    if token.startswith("public:"):
        return token
    if _credential_name(token) or _looks_like_opaque_public_token(token):
        # Persist a typed digest instead of a credential-shaped or otherwise
        # opaque identity token. This keeps the public launch receipt stable
        # without pretending we can classify every arbitrary argv secret value.
        return _public_identity_digest(token, kind=kind)
    return token


def _public_route_label(value: object, field: str) -> str:
    token = _public_token(value, field)
    if _credential_name(token) or _looks_like_opaque_public_token(token):
        raise AgentSafetyError("credential_isolation_unavailable")
    return token


def _public_metadata_token(value: object, field: str) -> str:
    if field in _PUBLIC_IDENTITY_FIELD_KINDS:
        return _public_identity_token(value, field)
    if field in _PUBLIC_ROUTE_LABEL_FIELDS:
        return _public_route_label(value, field)
    token = _public_token(value, field)
    if _credential_name(token) or _looks_like_opaque_public_token(token):
        raise AgentSafetyError("credential_isolation_unavailable")
    return token


def _uses_env_split_string(command: Sequence[str]) -> bool:
    """Return whether env would reparse an opaque split-string payload."""
    for index, token in enumerate(command):
        if PurePosixPath(token).name.lower() != "env":
            continue
        for argument in command[index + 1 :]:
            candidate = argument.strip()
            if candidate == "--":
                break
            if candidate == "--split-string" or candidate.startswith("--split-string="):
                return True
            if candidate.startswith("-") and not candidate.startswith("--"):
                if "S" in candidate[1:]:
                    return True
                continue
            if "=" in candidate and not candidate.startswith("="):
                continue
            break
    return False


def _uses_shell_command_string(command: Sequence[str]) -> bool:
    """Return whether a recognized shell would evaluate an inline program."""
    # Inspect exact interpreter tokens at every argv position so dispatchers
    # such as `env` and `busybox` cannot hide a shell command string. We stop
    # only at an explicit `--`; this deliberately rejects a later `-c` even
    # when a partial shell parser might classify it as a script argument.
    for index, token in enumerate(command):
        if PurePosixPath(token).name.lower() not in _POSIX_SHELL_INTERPRETERS:
            continue
        for argument in command[index + 1 :]:
            if argument == "--":
                break
            if (
                argument.startswith("-")
                and not argument.startswith("--")
                and argument != "-"
                and "c" in argument[1:]
            ):
                return True
    return False


def _credential_value_flag(argument: str, *, executable: str | None) -> bool:
    flag = argument.strip().split("=", 1)[0]
    if flag.startswith("-") and _credential_name(flag):
        return True
    if executable != "curl":
        return False
    return (
        flag in _CURL_CREDENTIAL_VALUE_FLAGS
        or flag in _CURL_CREDENTIAL_SOURCE_FLAGS
        or any(
            argument.startswith(short_flag) and argument != short_flag
            for short_flag in _CURL_ATTACHED_CREDENTIAL_FLAGS
        )
    )


def _sensitive_curl_header(value: object) -> bool:
    if not isinstance(value, str) or value.startswith("@"):
        return True
    name, separator, _ = value.partition(":")
    if not separator:
        return True
    return name.strip().lower() in _SENSITIVE_HEADER_NAMES


def _parse_nested_solver_command(value: object) -> list[str]:
    if not isinstance(value, str) or len(value) > _MAX_NESTED_COMMAND_JSON_CHARS:
        raise AgentSafetyError("credential_isolation_unavailable")
    try:
        nested_command = json.loads(value)
    except (json.JSONDecodeError, RecursionError):
        raise AgentSafetyError("credential_isolation_unavailable") from None
    if (
        isinstance(nested_command, (str, bytes))
        or not isinstance(nested_command, Sequence)
        or not nested_command
        or not all(
            isinstance(argument, str) and argument for argument in nested_command
        )
    ):
        raise AgentSafetyError("credential_isolation_unavailable")
    parsed = list(nested_command)
    if _uses_env_split_string(parsed):
        raise AgentSafetyError("external_command_env_split_string_unavailable")
    if _uses_shell_command_string(parsed):
        raise AgentSafetyError("external_command_shell_string_unavailable")
    return parsed


def _contains_inline_credential(
    value: object,
    *,
    depth: int = 0,
    budget: _CredentialScanBudget | None = None,
) -> bool:
    budget = budget or _CredentialScanBudget()
    if depth > _MAX_CREDENTIAL_SCAN_DEPTH or not budget.consume():
        return True
    if isinstance(value, Mapping):
        return any(
            (isinstance(key, str) and _credential_name(key))
            or _contains_inline_credential(item, depth=depth + 1, budget=budget)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = list(value)
        executable = (
            PurePosixPath(items[0].strip()).name.lower()
            if items and isinstance(items[0], str)
            else None
        )
        # A command dispatcher can move the effective executable away from
        # argv[0]. Recognize the exact curl executable token wherever it occurs
        # and apply the existing curl credential contract to its trailing argv.
        curl_index = next(
            (
                index
                for index, item in enumerate(items)
                if isinstance(item, str)
                and PurePosixPath(item.strip()).name.lower() == "curl"
            ),
            None,
        )
        nested_value_index: int | None = None
        for index, item in enumerate(items):
            if index == nested_value_index:
                continue
            if isinstance(item, str):
                candidate = item.strip()
                flag, separator, inline_value = candidate.partition("=")
                effective_executable = (
                    "curl"
                    if curl_index is not None and index > curl_index
                    else executable
                )
                if _credential_value_flag(flag, executable=effective_executable):
                    return True
                attached_header = (
                    candidate[2:]
                    if effective_executable == "curl"
                    and candidate.startswith("-H")
                    and candidate != "-H"
                    else None
                )
                if effective_executable == "curl" and (
                    flag in _CURL_HEADER_VALUE_FLAGS or attached_header is not None
                ):
                    header = (
                        attached_header
                        if attached_header is not None
                        else inline_value
                        if separator
                        else items[index + 1]
                        if index + 1 < len(items)
                        else None
                    )
                    if _sensitive_curl_header(header):
                        return True
                    if attached_header is None and not separator:
                        nested_value_index = index + 1
                    continue
                if flag in _NESTED_COMMAND_JSON_FLAGS:
                    nested_json = (
                        inline_value
                        if separator
                        else items[index + 1]
                        if index + 1 < len(items) and isinstance(items[index + 1], str)
                        else None
                    )
                    nested_command = _parse_nested_solver_command(nested_json)
                    if _contains_inline_credential(
                        nested_command, depth=depth + 1, budget=budget
                    ):
                        return True
                    if not separator:
                        nested_value_index = index + 1
                    continue
            if _contains_inline_credential(item, depth=depth + 1, budget=budget):
                return True
        return False
    if not isinstance(value, str):
        return False

    candidate = value.strip()
    if (candidate.startswith("-") or "=" in candidate) and _credential_name(candidate):
        return True
    if _AUTHORIZATION_HEADER_PATTERN.search(candidate):
        return True
    if _CREDENTIAL_HEADER_PATTERN.search(candidate):
        return True
    if _CREDENTIAL_ASSIGNMENT_PATTERN.search(candidate):
        return True
    if _URL_USERINFO_PATTERN.search(candidate):
        return True
    if candidate.startswith(("[", "{")):
        if len(candidate) > _MAX_NESTED_COMMAND_JSON_CHARS:
            return True
        try:
            nested = json.loads(candidate)
        except (json.JSONDecodeError, RecursionError):
            return False
        return _contains_inline_credential(nested, depth=depth + 1, budget=budget)
    return False


def _committed_solver_command_argument_count(command: Sequence[str]) -> int:
    items = list(command)
    for index, item in enumerate(items):
        candidate = item.strip()
        flag, separator, inline_value = candidate.partition("=")
        if flag not in _NESTED_COMMAND_JSON_FLAGS:
            continue
        nested_json = (
            inline_value
            if separator
            else items[index + 1]
            if index + 1 < len(items) and isinstance(items[index + 1], str)
            else None
        )
        return len(_parse_nested_solver_command(nested_json))
    return len(command)


def _validated_command(value: object) -> list[str]:
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, Sequence)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise ValueError("ExternalCommandAgent requires a non-empty command argv.")
    command = list(value)
    if _uses_env_split_string(command):
        raise AgentSafetyError("external_command_env_split_string_unavailable")
    if _uses_shell_command_string(command):
        raise AgentSafetyError("external_command_shell_string_unavailable")
    if _contains_inline_credential(command):
        # Keep this error independent of the rejected argv so it is safe to expose.
        raise AgentSafetyError("credential_isolation_unavailable")
    return command


def public_external_command_metadata(value: object) -> dict[str, object]:
    """Describe an argv without persisting any of its raw values."""
    command = _validated_command(value)
    return {
        "command_recorded": False,
        "command_argument_count": len(command),
        "command_sha256": _canonical_digest(command),
    }


def _containment_receipt_ref(generation: tuple[str, str]) -> str:
    container_id, started_at = generation
    generation_key = "\0".join((container_id, started_at))
    return f"loopsbench-container:{_sha256(generation_key)[:24]}"


def _clear_external_agent_result(container: Any) -> None:
    try:
        result_path = shlex.quote(_CONTAINER_RESULT_PATH)
        exit_code, _ = container.exec_run(
            ["sh", "-c", f"rm -f -- {result_path}"], demux=True
        )
        cleared = int(exit_code) == 0
    except Exception as exc:
        raise AgentSafetyError(
            "External command stale artifacts could not be cleared."
        ) from exc
    if not cleared:
        raise AgentSafetyError("External command stale artifacts could not be cleared.")


def _write_public_admission_state(
    logging_dir: Path | None,
    *,
    states: tuple[str, ...],
    launch_admission: Mapping[str, object],
) -> None:
    """Publish a redacted audit log without trusting it as runner state."""
    if logging_dir is None:
        return
    try:
        logging_dir.mkdir(parents=True, exist_ok=True)
        (logging_dir / "launch_admission_state.json").write_text(
            json.dumps(
                {
                    "schema_version": "loopsbench_launch_admission_state_v1",
                    "states": list(states),
                    "launch_admission": dict(launch_admission),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        raise AgentSafetyError("launch_admission_state_persist_failed") from exc


def _checked_copy_to_container(
    container: Any,
    *,
    paths: Sequence[Path],
    container_dir: str,
) -> None:
    """Deliver runner files while checking every Docker operation."""
    try:
        exit_code, _ = container.exec_run(["mkdir", "-p", container_dir])
        if int(exit_code) != 0:
            raise AgentSafetyError("launch_admission_delivery_failed")
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as bundle:
            for path in paths:
                bundle.add(path, arcname=path.name, recursive=False)
        transferred = container.put_archive(container_dir, archive.getvalue())
        if transferred is not True:
            raise AgentSafetyError("launch_admission_delivery_failed")
    except AgentSafetyError:
        raise
    except Exception as exc:
        raise AgentSafetyError("launch_admission_delivery_failed") from exc


def _archive_error(kind: str, reason: str) -> AgentSafetyError:
    return AgentSafetyError(f"external_agent_{kind}_archive_{reason}")


def _validate_member_name(name: str, *, kind: str) -> None:
    parts = PurePosixPath(name).parts
    if (
        not name
        or PurePosixPath(name).is_absolute()
        or "\x00" in name
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise _archive_error(kind, "invalid")


def _validate_link_target(member: tarfile.TarInfo, *, kind: str) -> None:
    if not (member.issym() or member.islnk()):
        return
    target = PurePosixPath(member.linkname)
    if target.is_absolute():
        raise _archive_error(kind, "invalid")
    base = PurePosixPath(member.name).parent if member.issym() else PurePosixPath()
    resolved: list[str] = []
    for part in (*base.parts, *target.parts):
        if part in ("", "."):
            continue
        if part == "..":
            if not resolved:
                raise _archive_error(kind, "invalid")
            resolved.pop()
        else:
            resolved.append(part)
    if not resolved or resolved[0] != "workspace":
        raise _archive_error(kind, "invalid")


def _validate_archive(
    archive: bytes,
    *,
    kind: str,
    max_expanded_bytes: int,
    max_members: int,
    max_member_bytes: int,
) -> None:
    member_count = 0
    expanded_bytes = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as bundle:
            for member in bundle:
                member_count += 1
                if member_count > max_members:
                    raise _archive_error(kind, "member_limit_exceeded")
                _validate_member_name(member.name, kind=kind)
                if kind == "workspace" and PurePosixPath(member.name).parts[0] != (
                    "workspace"
                ):
                    raise _archive_error(kind, "invalid")
                if (
                    getattr(member, "sparse", None)
                    or member.type == tarfile.GNUTYPE_SPARSE
                    or any(
                        str(key).startswith(("GNU.sparse", "SCHILY.realsize"))
                        for key in member.pax_headers
                    )
                ):
                    raise _archive_error(kind, "sparse_member_rejected")
                if not (
                    member.isfile()
                    or member.isdir()
                    or member.issym()
                    or member.islnk()
                ):
                    raise _archive_error(kind, "invalid")
                _validate_link_target(member, kind=kind)
                if member.size < 0:
                    raise _archive_error(kind, "invalid")
                if member.isfile() and member.size > max_member_bytes:
                    raise _archive_error(kind, "member_limit_exceeded")
                expanded_bytes += member.size
                if expanded_bytes > max_expanded_bytes:
                    raise _archive_error(kind, "expanded_limit_exceeded")
    except AgentSafetyError:
        raise
    except (OSError, tarfile.TarError, ValueError) as exc:
        raise _archive_error(kind, "invalid") from exc


def validate_external_workspace_archive(archive: bytes) -> None:
    """Validate a byte-exact workspace archive before it crosses boundaries."""
    if not archive or len(archive) > _MAX_WORKSPACE_ARCHIVE_BYTES:
        raise _archive_error("workspace", "stream_limit_exceeded")
    _validate_archive(
        archive,
        kind="workspace",
        max_expanded_bytes=_MAX_WORKSPACE_EXPANDED_BYTES,
        max_members=_MAX_WORKSPACE_ARCHIVE_MEMBERS,
        max_member_bytes=_MAX_WORKSPACE_MEMBER_BYTES,
    )


def _capture_archive(
    container: Any,
    path: str,
    error: str,
    *,
    kind: str,
    max_stream_bytes: int,
    max_expanded_bytes: int,
    max_members: int,
    max_member_bytes: int,
) -> bytes:
    try:
        chunks, _ = container.get_archive(path)
        buffer = io.BytesIO()
        stream_bytes = 0
        for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise TypeError("archive chunk is not bytes")
            stream_bytes += len(chunk)
            if stream_bytes > max_stream_bytes:
                raise _archive_error(kind, "stream_limit_exceeded")
            buffer.write(chunk)
        archive = buffer.getvalue()
    except AgentSafetyError:
        raise
    except Exception as exc:
        raise AgentSafetyError(error) from exc
    if not archive:
        raise AgentSafetyError(error)
    _validate_archive(
        archive,
        kind=kind,
        max_expanded_bytes=max_expanded_bytes,
        max_members=max_members,
        max_member_bytes=max_member_bytes,
    )
    return archive


def _run_fixed_launch(
    session: TmuxSession, *, container: Any, timeout_sec: float
) -> None:
    """Launch through tmux without logging argv or private wait markers."""
    try:
        exit_code, _ = container.exec_run(
            ["rm", "-f", _CONTAINER_LAUNCH_DONE_PATH], demux=True
        )
        if int(exit_code) != 0:
            raise AgentSafetyError("external_agent_launch_failed")
        session._append_command_log(f"{_PUBLIC_LAUNCH_COMMAND} Enter")
        exit_code, _ = container.exec_run(
            [
                "tmux",
                "send-keys",
                "-t",
                session._session_name,
                _PUBLIC_LAUNCH_COMMAND,
                "Enter",
            ],
            demux=True,
        )
        if int(exit_code) != 0:
            raise AgentSafetyError("external_agent_launch_failed")
    except AgentSafetyError:
        raise
    except Exception as exc:
        raise AgentSafetyError("external_agent_launch_failed") from exc

    deadline = datetime.now(timezone.utc).timestamp() + timeout_sec
    while datetime.now(timezone.utc).timestamp() < deadline:
        try:
            exit_code, _ = container.exec_run(
                ["test", "-f", _CONTAINER_LAUNCH_DONE_PATH], demux=True
            )
        except Exception as exc:
            raise AgentSafetyError("external_agent_launch_wait_failed") from exc
        if int(exit_code) == 0:
            return
        if int(exit_code) != 1:
            raise AgentSafetyError("external_agent_launch_wait_failed")
        # Match the existing tmux polling cadence without exposing its marker.
        time.sleep(0.5)
    raise TimeoutError("external agent timed out")


def _verify_paused_generation(
    container: Any, *, expected_generation: tuple[str, str]
) -> None:
    try:
        container.pause()
        container.reload()
        attrs = container.attrs
        state = attrs.get("State") if isinstance(attrs, Mapping) else None
        generation = (
            (attrs.get("Id"), state.get("StartedAt"))
            if isinstance(state, Mapping)
            else (None, None)
        )
    except Exception as exc:
        raise AgentSafetyError("external_agent_snapshot_pause_failed") from exc
    if (
        not isinstance(state, Mapping)
        or state.get("Paused") is not True
        or generation != expected_generation
    ):
        raise AgentSafetyError("external_agent_snapshot_pause_unverified")


def _result_bytes_from_archive(archive: bytes) -> bytes:
    if not archive or len(archive) > _MAX_RESULT_ARCHIVE_BYTES:
        raise _archive_error("result", "stream_limit_exceeded")
    _validate_archive(
        archive,
        kind="result",
        max_expanded_bytes=_MAX_RESULT_JSON_BYTES,
        max_members=_MAX_RESULT_ARCHIVE_MEMBERS,
        max_member_bytes=_MAX_RESULT_JSON_BYTES,
    )
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as bundle:
            files = [member for member in bundle.getmembers() if member.isfile()]
            if len(files) != 1 or Path(files[0].name).name != "result.json":
                raise AgentSafetyError("external_agent_result_invalid")
            extracted = bundle.extractfile(files[0])
            if extracted is None:
                raise AgentSafetyError("external_agent_result_invalid")
            raw = extracted.read(_MAX_RESULT_JSON_BYTES + 1)
    except AgentSafetyError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise AgentSafetyError("external_agent_result_invalid") from exc
    if len(raw) > _MAX_RESULT_JSON_BYTES:
        raise _archive_error("result", "json_limit_exceeded")
    if not raw:
        raise AgentSafetyError("external_agent_result_missing")
    return raw


class ExternalCommandAgent(BaseAgent):
    """Run a preinstalled command against a LoopsBench task workspace."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        benchmark_id: str = "loopsbench",
        case_id: str,
        run_id: str,
        arm_id: str,
        expected_provider: str,
        expected_model: str,
        provider_authority: str,
        integrity_policy_sha256: str,
        resolved_image_id: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self._command = _validated_command(command)
        self._committed_solver_command_argument_count = (
            _committed_solver_command_argument_count(self._command)
        )
        self._launch_identity = {
            "benchmark_id": _public_metadata_token(benchmark_id, "benchmark_id"),
            "case_id": _public_metadata_token(case_id, "case_id"),
            "run_id": _public_metadata_token(run_id, "run_id"),
            "arm_id": _public_metadata_token(arm_id, "arm_id"),
            "expected_provider": _public_metadata_token(
                expected_provider, "expected_provider"
            ),
            "expected_model": _public_metadata_token(expected_model, "expected_model"),
            "provider_authority": _public_metadata_token(
                provider_authority, "provider_authority"
            ),
            "integrity_policy_sha256": integrity_policy_sha256,
            "resolved_image_id": resolved_image_id,
        }
        if len(integrity_policy_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in integrity_policy_sha256
        ):
            raise AgentSafetyError("launch_binding_integrity_policy_sha256_invalid")

    @classmethod
    def validate_preseed(cls, kwargs: Mapping[str, object]) -> None:
        """Validate public identities and reject inline credential channels."""
        for field in (
            "benchmark_id",
            "case_id",
            "run_id",
            "arm_id",
            "expected_provider",
            "expected_model",
            "provider_authority",
        ):
            _public_metadata_token(kwargs.get(field), field)
        policy_digest = kwargs.get("integrity_policy_sha256")
        if (
            not isinstance(policy_digest, str)
            or len(policy_digest) != 64
            or any(char not in "0123456789abcdef" for char in policy_digest)
        ):
            raise AgentSafetyError("launch_binding_integrity_policy_sha256_invalid")
        if any(
            key != "command" and isinstance(key, str) and _credential_name(key)
            for key in kwargs
        ):
            raise AgentSafetyError("credential_isolation_unavailable")
        _validated_command(kwargs.get("command"))

    def _launch_admission(
        self, instruction: str, evidence: EffectiveContainerEvidence
    ) -> dict[str, object]:
        expected_image_id = self._launch_identity["resolved_image_id"]
        if expected_image_id is not None and evidence.image_id != expected_image_id:
            raise AgentSafetyError("effective_container_image_mismatch")
        containment_private = {
            "container_id": evidence.container_id,
            "started_at": evidence.started_at,
        }
        runtime_private = {
            **containment_private,
            "image_id": evidence.image_id,
        }
        isolation_evidence = {
            "credential_policy": "excluded",
            "effective_container_validated": True,
        }
        receipt: dict[str, object] = {
            "schema_version": BENCHMARK_LAUNCH_ADMISSION_RECEIPT_SCHEMA_VERSION,
            "benchmark_id": self._launch_identity["benchmark_id"],
            "case_id": self._launch_identity["case_id"],
            "run_id": self._launch_identity["run_id"],
            "arm_id": self._launch_identity["arm_id"],
            "instruction_sha256": _sha256(instruction),
            "integrity_policy_sha256": self._launch_identity["integrity_policy_sha256"],
            "expected_route": {
                "provider": self._launch_identity["expected_provider"],
                "model": self._launch_identity["expected_model"],
            },
            "containment_binding_sha256": _sha256(
                _containment_receipt_ref((evidence.container_id, evidence.started_at))
            ),
            "runtime_binding_sha256": _canonical_digest(runtime_private),
            "credential_isolation": {
                "mechanism": "environment_exclusion",
                "authority": "loopsbench",
                "evidence_sha256": _canonical_digest(isolation_evidence),
            },
            "controller_isolation": {
                "mechanism": "container_boundary",
                "authority": "loopsbench",
                "evidence_sha256": _canonical_digest(
                    {"docker_socket_absent": True, "host_pid_absent": True}
                ),
            },
            "runner_authority": "loopsbench",
            "provider_authority": self._launch_identity["provider_authority"],
            "issued_at": datetime.now(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            "public_boundary": dict(_PUBLIC_BOUNDARY),
        }
        digest_payload = {
            key: value
            for key, value in receipt.items()
            if key not in {"launch_binding_digest", "public_boundary"}
        }
        receipt["launch_binding_digest"] = _canonical_digest(digest_payload)
        return receipt

    @staticmethod
    def name() -> str:
        return "external-command"

    def get_trajectory_paths(self) -> list[str]:
        # The validated public result is runner-owned host evidence. It must not
        # be copied back into the solver container and re-extracted as a trajectory.
        return []

    @property
    def snapshot(self) -> ExternalCommandSnapshot | None:
        """Return the process-private snapshot marker for Harness handoff."""
        return getattr(self, "_snapshot", None)

    @staticmethod
    def _request(
        instruction: str,
        timeout_sec: float,
        *,
        containment_receipt_ref: str,
        launch_admission: Mapping[str, object],
    ) -> dict[str, object]:
        return {
            "schema_version": EXTERNAL_AGENT_REQUEST_SCHEMA_VERSION,
            "instruction": instruction,
            "workspace": "/workspace",
            "timeout_seconds": timeout_sec,
            "launch_admission": dict(launch_admission),
            "containment": {
                "schema_version": EXTERNAL_AGENT_CONTAINMENT_SCHEMA_VERSION,
                "kind": "container",
                "timeout_owner": "runner",
                "termination_postcondition": _CONTAINMENT_POSTCONDITION,
                "verification": {
                    "schema_version": (
                        EXTERNAL_AGENT_CONTAINMENT_VERIFICATION_SCHEMA_VERSION
                    ),
                    "status": "pending",
                    "authority": "runner",
                    "receipt_ref": containment_receipt_ref,
                },
            },
        }

    @staticmethod
    def _compact_result(
        value: object,
        *,
        instruction: str,
        launch_binding_digest: str,
        committed_command_argument_count: int,
        duration_ms: int,
    ) -> dict[str, object]:
        if not isinstance(value, Mapping):
            raise AgentSafetyError("external_agent_result_invalid")
        if set(value) != {"schema_version", "status", "exit_code", "receipt"}:
            raise AgentSafetyError("external_agent_result_invalid")
        if value.get("schema_version") != EXTERNAL_AGENT_RESULT_SCHEMA_VERSION:
            raise AgentSafetyError("external_agent_result_invalid")
        status = value.get("status")
        exit_code = value.get("exit_code")
        if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)
        ):
            raise AgentSafetyError("external_agent_result_invalid")
        receipt = value.get("receipt")
        if not isinstance(receipt, Mapping) or set(receipt) != _RECEIPT_KEYS:
            raise AgentSafetyError("external_agent_result_invalid")
        schema_version = receipt.get("schema_version")
        classification = receipt.get("classification")
        if schema_version != _RECEIPT_SCHEMA_VERSION:
            raise AgentSafetyError("external_agent_result_invalid")
        matrix_valid = (
            (
                classification == "solver_completed"
                and status == "succeeded"
                and exit_code == 0
            )
            or (
                classification == "solver_exited_nonzero"
                and status == "failed"
                and isinstance(exit_code, int)
                and not isinstance(exit_code, bool)
                and exit_code != 0
            )
            or (
                classification == "solver_startup_failed"
                and status == "failed"
                and exit_code is None
            )
        )
        if not matrix_valid:
            raise AgentSafetyError("external_agent_result_invalid")
        solver_command_argument_count = receipt.get("command_argument_count")
        solver_duration_ms = receipt.get("duration_ms")
        if (
            isinstance(committed_command_argument_count, bool)
            or not isinstance(committed_command_argument_count, int)
            or committed_command_argument_count <= 0
            or isinstance(solver_command_argument_count, bool)
            or not isinstance(solver_command_argument_count, int)
            or solver_command_argument_count <= 0
            or isinstance(solver_duration_ms, bool)
            or not isinstance(solver_duration_ms, int)
            or solver_duration_ms < 0
            or receipt.get("command_recorded") is not False
            or receipt.get("instruction_recorded") is not False
            or receipt.get("workspace_recorded") is not False
        ):
            raise AgentSafetyError("external_agent_result_invalid")
        if solver_command_argument_count != committed_command_argument_count:
            raise AgentSafetyError("external_agent_result_binding_mismatch")
        compact_receipt: dict[str, object] = {
            "schema_version": schema_version,
            "classification": classification,
            "command_recorded": False,
            "command_argument_count": committed_command_argument_count,
            "duration_ms": duration_ms,
            "instruction_recorded": False,
            "workspace_recorded": False,
        }
        instruction_sha256 = receipt.get("instruction_sha256")
        if (
            not isinstance(instruction_sha256, str)
            or len(instruction_sha256) != 64
            or any(char not in "0123456789abcdef" for char in instruction_sha256)
        ):
            raise AgentSafetyError("external_agent_result_invalid")
        if instruction_sha256 != _sha256(instruction):
            raise AgentSafetyError("external_agent_result_binding_mismatch")
        compact_receipt["instruction_sha256"] = instruction_sha256
        instruction_chars = receipt.get("instruction_chars")
        if isinstance(instruction_chars, bool) or not isinstance(
            instruction_chars, int
        ):
            raise AgentSafetyError("external_agent_result_invalid")
        if instruction_chars != len(instruction):
            raise AgentSafetyError("external_agent_result_binding_mismatch")
        compact_receipt["instruction_chars"] = instruction_chars
        if receipt.get("launch_binding_digest") != launch_binding_digest:
            raise AgentSafetyError("external_agent_result_binding_mismatch")
        compact_receipt["launch_binding_digest"] = launch_binding_digest
        return {
            "schema_version": EXTERNAL_AGENT_RESULT_SCHEMA_VERSION,
            "status": status,
            "exit_code": exit_code,
            "receipt": compact_receipt,
        }

    @classmethod
    def _read_result(
        cls,
        raw_result_archive: bytes,
        *,
        instruction: str,
        launch_binding_digest: str,
        committed_command_argument_count: int,
        duration_ms: int,
    ) -> dict[str, object]:
        try:
            raw = _result_bytes_from_archive(raw_result_archive)
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AgentSafetyError("external_agent_result_invalid") from None
        except AgentSafetyError:
            raise
        except Exception as exc:
            raise AgentSafetyError("external_agent_result_read_failed") from exc
        return cls._compact_result(
            value,
            instruction=instruction,
            launch_binding_digest=launch_binding_digest,
            committed_command_argument_count=committed_command_argument_count,
            duration_ms=duration_ms,
        )

    def finalize_snapshot(self, snapshot: ExternalCommandSnapshot) -> AgentResult:
        """Parse runner-owned bytes only after Harness proves client removal."""
        if snapshot.snapshot_error is not None:
            raise snapshot.snapshot_error
        if snapshot.execution_error is not None:
            raise AgentSafetyError("external_agent_execution_failed") from (
                snapshot.execution_error
            )
        if snapshot.timed_out:
            return AgentResult(failure_mode=FailureMode.AGENT_TIMEOUT.value)
        if snapshot.raw_result_archive is None:
            raise AgentSafetyError("external_agent_result_missing")
        result = self._read_result(
            snapshot.raw_result_archive,
            instruction=snapshot.instruction,
            launch_binding_digest=snapshot.launch_binding_digest,
            committed_command_argument_count=snapshot.command_argument_count,
            duration_ms=snapshot.duration_ms,
        )
        self._persist_compact_receipt(snapshot.logging_dir, result)
        return AgentResult(failure_mode=self._failure_mode(result))

    @staticmethod
    def persist_containment_absence_proof(
        snapshot: ExternalCommandSnapshot, *, container_name: str
    ) -> None:
        """Persist a public digest only after the harness proves absence."""
        evidence_preimage = {
            "container_absent": True,
            "container_id": snapshot.container_id,
            "container_name": container_name,
            "containment_receipt_ref": snapshot.containment_receipt_ref,
        }
        proof = {
            "schema_version": _CONTAINMENT_PROOF_SCHEMA_VERSION,
            "authority": "loopsbench",
            "launch_binding_digest": snapshot.launch_binding_digest,
            "containment_binding_sha256": snapshot.launch_admission[
                "containment_binding_sha256"
            ],
            "containment_termination_postcondition": _CONTAINMENT_POSTCONDITION,
            "containment_absence_verified": True,
            "containment_absence_evidence_sha256": _canonical_digest(evidence_preimage),
        }
        temporary_path: Path | None = None
        try:
            receipt_dir = snapshot.logging_dir / "trajectories"
            receipt_dir.mkdir(parents=True, exist_ok=True)
            proof_path = receipt_dir / "containment.public.json"
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=receipt_dir,
                prefix=".containment.public.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(proof, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, proof_path)
            _write_public_admission_state(
                snapshot.logging_dir,
                states=(
                    "launch_inputs_validated",
                    "effective_container_admitted",
                    "containment_destroyed_before_result",
                ),
                launch_admission=snapshot.launch_admission,
            )
        except Exception as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise AgentSafetyError("external_agent_containment_proof_failed") from exc

    @staticmethod
    def _persist_compact_receipt(
        logging_dir: Path | None, result: Mapping[str, object]
    ) -> None:
        if logging_dir is None:
            raise AgentSafetyError("external_agent_receipt_persist_failed")
        temporary_path: Path | None = None
        try:
            receipt_dir = logging_dir / "trajectories"
            receipt_dir.mkdir(parents=True, exist_ok=True)
            receipt_path = receipt_dir / "result.public.json"
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=receipt_dir,
                prefix=".result.public.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(dict(result), handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, receipt_path)
        except Exception as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise AgentSafetyError("external_agent_receipt_persist_failed") from exc

    @staticmethod
    def _failure_mode(result: Mapping[str, object]) -> str:
        receipt = result["receipt"]
        assert isinstance(receipt, Mapping)
        classification = receipt["classification"]
        if classification == "solver_completed":
            return FailureMode.NONE.value
        if classification == "solver_startup_failed":
            return FailureMode.AGENT_STARTUP_ERROR.value
        return FailureMode.UNKNOWN_AGENT_ERROR.value

    def perform_task(
        self,
        instruction: str,
        session: TmuxSession,
        logging_dir: Path | None = None,
        timeout_sec: float | None = None,
    ) -> AgentResult:
        return self.perform_task_with_evidence(
            instruction,
            session,
            logging_dir=logging_dir,
            timeout_sec=timeout_sec,
        )

    def perform_task_with_evidence(
        self,
        instruction: str,
        session: TmuxSession,
        *,
        logging_dir: Path | None = None,
        timeout_sec: float | None = None,
        effective_container_evidence: EffectiveContainerEvidence | None = None,
    ) -> AgentResult:
        if logging_dir is None:
            raise AgentSafetyError("external_agent_receipt_persist_failed")
        self._snapshot = None
        effective_timeout = timeout_sec or 86_400.0
        container = session._container  # type: ignore[attr-defined]
        try:
            evidence = effective_container_evidence or (
                DockerComposeManager.inspect_effective_container(
                    container, external_command_isolation=True
                )
            )
        except ContainerAdmissionError as exc:
            raise AgentSafetyError(str(exc)) from exc
        except Exception as exc:
            raise AgentSafetyError(
                "effective_container_inspection_unavailable"
            ) from exc
        generation = (evidence.container_id, evidence.started_at)
        containment_receipt_ref = _containment_receipt_ref(generation)
        try:
            launch_admission = self._launch_admission(instruction, evidence)
            launch_binding_digest = str(launch_admission["launch_binding_digest"])
            admission_states = (
                "launch_inputs_validated",
                "effective_container_admitted",
            )
            _write_public_admission_state(
                logging_dir,
                states=admission_states,
                launch_admission=launch_admission,
            )
            request = self._request(
                instruction,
                effective_timeout,
                containment_receipt_ref=containment_receipt_ref,
                launch_admission=launch_admission,
            )
            with tempfile.TemporaryDirectory(
                prefix="loopsbench-external-command-"
            ) as root:
                request_path = Path(root) / "request.json"
                request_path.write_text(
                    json.dumps(request, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                launch_path = Path(root) / "launch.sh"
                launch_command = shlex.join(self._command)
                environment = (
                    f"LOOPSBENCH_EXTERNAL_AGENT_REQUEST="
                    f"{shlex.quote(_CONTAINER_REQUEST_PATH)} "
                    f"LOOPSBENCH_EXTERNAL_AGENT_RESULT="
                    f"{shlex.quote(_CONTAINER_RESULT_PATH)} "
                    f"LOOPSBENCH_EXTERNAL_AGENT_WORKSPACE=/workspace "
                    f"LOOPSBENCH_EXTERNAL_AGENT_TIMEOUT_SECONDS="
                    f"{int(effective_timeout)}"
                )
                launch_path.write_text(
                    "#!/bin/sh\n"
                    "set +e\n"
                    f"env {environment} {launch_command}\n"
                    "status=$?\n"
                    f": > {shlex.quote(_CONTAINER_LAUNCH_DONE_PATH)}\n"
                    'exit "$status"\n',
                    encoding="utf-8",
                )
                _checked_copy_to_container(
                    container,
                    paths=(request_path, launch_path),
                    container_dir=str(Path(_CONTAINER_REQUEST_PATH).parent),
                )
        except AgentSafetyError:
            raise
        except Exception as exc:
            raise AgentSafetyError("launch_admission_delivery_failed") from exc

        _clear_external_agent_result(container)
        started_at = time.monotonic()
        timed_out = False
        execution_error: Exception | None = None
        try:
            _run_fixed_launch(
                session, container=container, timeout_sec=effective_timeout
            )
        except TimeoutError:
            timed_out = True
        except Exception as exc:
            execution_error = exc
        duration_ms = max(0, int((time.monotonic() - started_at) * 1000))
        snapshot_error: AgentSafetyError | None = None
        raw_result_archive: bytes | None = None
        workspace_archive: bytes | None = None
        paused = False
        try:
            _verify_paused_generation(container, expected_generation=generation)
            paused = True
        except AgentSafetyError as exc:
            snapshot_error = snapshot_error or exc
        if paused and not timed_out and execution_error is None:
            try:
                raw_result_archive = _capture_archive(
                    container,
                    _CONTAINER_RESULT_PATH,
                    "external_agent_result_capture_failed",
                    kind="result",
                    max_stream_bytes=_MAX_RESULT_ARCHIVE_BYTES,
                    max_expanded_bytes=_MAX_RESULT_JSON_BYTES,
                    max_members=_MAX_RESULT_ARCHIVE_MEMBERS,
                    max_member_bytes=_MAX_RESULT_JSON_BYTES,
                )
            except AgentSafetyError as exc:
                snapshot_error = snapshot_error or exc
        if paused:
            try:
                workspace_archive = _capture_archive(
                    container,
                    "/workspace",
                    "external_agent_workspace_capture_failed",
                    kind="workspace",
                    max_stream_bytes=_MAX_WORKSPACE_ARCHIVE_BYTES,
                    max_expanded_bytes=_MAX_WORKSPACE_EXPANDED_BYTES,
                    max_members=_MAX_WORKSPACE_ARCHIVE_MEMBERS,
                    max_member_bytes=_MAX_WORKSPACE_MEMBER_BYTES,
                )
            except AgentSafetyError as exc:
                snapshot_error = snapshot_error or exc
        self._snapshot = ExternalCommandSnapshot(
            container_id=evidence.container_id,
            post_agent_pane=(
                "External solver pane output was not retained by LoopsBench.\n"
            ),
            workspace_archive=workspace_archive,
            raw_result_archive=raw_result_archive,
            timed_out=timed_out,
            execution_error=execution_error,
            snapshot_error=snapshot_error,
            instruction=instruction,
            containment_receipt_ref=containment_receipt_ref,
            launch_binding_digest=launch_binding_digest,
            launch_admission=launch_admission,
            logging_dir=logging_dir,
            command_argument_count=self._committed_solver_command_argument_count,
            duration_ms=duration_ms,
        )
        # Harness owns the irreversible remove boundary. No result bytes are
        # decoded and no public success receipt is emitted before that proof.
        return AgentResult(failure_mode=FailureMode.NONE.value)
