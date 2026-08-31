"""Provider-neutral external command agent.

The command must already be available inside the task container. LoopsBench
passes the task instruction through a small JSON request and continues to own
the task container, verifier, and official score.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from loopsbench.agents.base_agent import AgentResult, AgentSafetyError, BaseAgent
from loopsbench.harness.models import FailureMode
from loopsbench.terminal.docker_compose_manager import DockerComposeManager
from loopsbench.terminal.tmux_session import TmuxSession

EXTERNAL_AGENT_REQUEST_SCHEMA_VERSION = "external_agent_request_v1"
EXTERNAL_AGENT_RESULT_SCHEMA_VERSION = "external_agent_result_v1"
EXTERNAL_AGENT_CONTAINMENT_SCHEMA_VERSION = "external_agent_containment_v1"
EXTERNAL_AGENT_CONTAINMENT_VERIFICATION_SCHEMA_VERSION = (
    "external_agent_containment_verification_v1"
)
_CONTAINER_REQUEST_PATH = "/tmp/loopsbench-external-command/request.json"
_CONTAINER_RESULT_PATH = "/tmp/loopsbench-external-command/result.json"
_CONTAINER_RECEIPT_PATH = "/agent-logs/external-command/result.public.json"
_CONTAINMENT_POSTCONDITION = "drained_before_result_consumption"
_CONTAINER_DRAIN_TIMEOUT_SECONDS = 0
_RESULT_STATUSES = {"succeeded", "failed"}
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
    "containment_contract_validated",
    "containment_kind",
    "containment_verification_authority",
    "containment_verification_status",
    "containment_termination_postcondition",
    "timeout_enforced_locally",
    "timeout_owner",
    "containment_verification_ref_sha256",
}


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _verified_container_generation(container: Any) -> tuple[str, str]:
    container.reload()
    attrs = container.attrs
    if not isinstance(attrs, Mapping):
        raise AgentSafetyError("External command container metadata is unavailable.")
    host_config = attrs.get("HostConfig")
    state = attrs.get("State")
    mounts = attrs.get("Mounts", [])
    if not isinstance(host_config, Mapping) or not isinstance(state, Mapping):
        raise AgentSafetyError(
            "External command container isolation is unverifiable."
        )
    if host_config.get("Privileged") is True or host_config.get("PidMode") not in (
        None,
        "",
    ):
        raise AgentSafetyError("External command container is not safely isolated.")
    if not isinstance(mounts, list) or any(
        isinstance(mount, Mapping)
        and "docker.sock"
        in f"{mount.get('Source', '')} {mount.get('Destination', '')}"
        for mount in mounts
    ):
        raise AgentSafetyError(
            "External command container exposes the Docker socket."
        )
    if state.get("Running") is not True:
        raise AgentSafetyError("External command container is not running.")
    container_id = attrs.get("Id") or getattr(container, "id", None)
    started_at = state.get("StartedAt")
    if not isinstance(container_id, str) or not container_id:
        raise AgentSafetyError("External command container id is unavailable.")
    if not isinstance(started_at, str) or not started_at:
        raise AgentSafetyError(
            "External command container generation is unavailable."
        )
    return container_id, started_at


def _containment_receipt_ref(generation: tuple[str, str]) -> str:
    container_id, started_at = generation
    generation_key = "\0".join((container_id, started_at))
    return f"loopsbench-container:{_sha256(generation_key)[:24]}"


def _stop_unverified_container(container: Any) -> None:
    try:
        container.kill()
    except Exception:
        pass


def _clear_external_agent_artifacts(container: Any) -> None:
    paths = " ".join(
        shlex.quote(path)
        for path in (_CONTAINER_RESULT_PATH, _CONTAINER_RECEIPT_PATH)
    )
    exit_code, _ = container.exec_run(
        ["sh", "-c", f"rm -f -- {paths}"], demux=True
    )
    if int(exit_code) != 0:
        raise AgentSafetyError(
            "External command stale artifacts could not be cleared."
        )


def _restart_and_verify_drained(
    container: Any, *, previous_generation: tuple[str, str]
) -> None:
    try:
        container.restart(timeout=_CONTAINER_DRAIN_TIMEOUT_SECONDS)
        current_generation = _verified_container_generation(container)
    except Exception as exc:
        _stop_unverified_container(container)
        raise AgentSafetyError(
            "External command containment could not be drained and verified."
        ) from exc
    if current_generation == previous_generation:
        _stop_unverified_container(container)
        raise AgentSafetyError(
            "External command containment generation did not change after restart."
        )


class ExternalCommandAgent(BaseAgent):
    """Run a preinstalled command against a LoopsBench task workspace."""

    def __init__(self, command: Sequence[str], **kwargs: Any):
        super().__init__(**kwargs)
        if (
            isinstance(command, (str, bytes))
            or not isinstance(command, Sequence)
            or not command
            or not all(isinstance(item, str) and item for item in command)
        ):
            raise ValueError("ExternalCommandAgent requires a non-empty command argv.")
        self._command = list(command)

    @staticmethod
    def name() -> str:
        return "external-command"

    def get_trajectory_paths(self) -> list[str]:
        return [_CONTAINER_RECEIPT_PATH]

    @staticmethod
    def _request(
        instruction: str, timeout_sec: float, *, containment_receipt_ref: str
    ) -> dict[str, object]:
        return {
            "schema_version": EXTERNAL_AGENT_REQUEST_SCHEMA_VERSION,
            "instruction": instruction,
            "workspace": "/workspace",
            "timeout_seconds": timeout_sec,
            "containment": {
                "schema_version": EXTERNAL_AGENT_CONTAINMENT_SCHEMA_VERSION,
                "kind": "container",
                "timeout_owner": "runner",
                "termination_postcondition": _CONTAINMENT_POSTCONDITION,
                "verification": {
                    "schema_version": (
                        EXTERNAL_AGENT_CONTAINMENT_VERIFICATION_SCHEMA_VERSION
                    ),
                    "status": "verified",
                    "authority": "runner",
                    "receipt_ref": containment_receipt_ref,
                },
            },
        }

    @staticmethod
    def _compact_result(
        value: object, *, instruction: str, containment_receipt_ref: str
    ) -> dict[str, object] | None:
        if not isinstance(value, Mapping):
            return None
        if set(value) != {"schema_version", "status", "exit_code", "receipt"}:
            return None
        if value.get("schema_version") != EXTERNAL_AGENT_RESULT_SCHEMA_VERSION:
            return None
        status = value.get("status")
        if not isinstance(status, str) or status not in _RESULT_STATUSES:
            return None
        exit_code = value.get("exit_code")
        if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)
        ):
            return None
        if (status == "succeeded") != (exit_code == 0):
            return None
        receipt = value.get("receipt")
        if not isinstance(receipt, Mapping) or set(receipt) - _RECEIPT_KEYS:
            return None
        schema_version = receipt.get("schema_version")
        classification = receipt.get("classification")
        if not all(
            isinstance(item, str) and 0 < len(item) <= 128
            for item in (schema_version, classification)
        ):
            return None
        command_argument_count = receipt.get("command_argument_count")
        duration_ms = receipt.get("duration_ms")
        if (
            isinstance(command_argument_count, bool)
            or not isinstance(command_argument_count, int)
            or command_argument_count < 1
            or isinstance(duration_ms, bool)
            or not isinstance(duration_ms, int)
            or duration_ms < 0
            or receipt.get("command_recorded") is not False
            or receipt.get("instruction_recorded") is not False
            or receipt.get("workspace_recorded") is not False
        ):
            return None
        compact_receipt: dict[str, object] = {
            "schema_version": schema_version,
            "classification": classification,
            "command_recorded": False,
            "command_argument_count": command_argument_count,
            "duration_ms": duration_ms,
            "instruction_recorded": False,
            "workspace_recorded": False,
        }
        instruction_sha256 = receipt.get("instruction_sha256")
        if (
            not isinstance(instruction_sha256, str)
            or len(instruction_sha256) != 64
            or any(char not in "0123456789abcdef" for char in instruction_sha256)
            or instruction_sha256 != _sha256(instruction)
        ):
            return None
        compact_receipt["instruction_sha256"] = instruction_sha256
        instruction_chars = receipt.get("instruction_chars")
        if (
            isinstance(instruction_chars, bool)
            or not isinstance(instruction_chars, int)
            or instruction_chars != len(instruction)
        ):
            return None
        compact_receipt["instruction_chars"] = instruction_chars
        containment_ref_sha256 = receipt.get(
            "containment_verification_ref_sha256"
        )
        if (
            receipt.get("containment_contract_validated") is not True
            or receipt.get("containment_kind") != "container"
            or receipt.get("containment_verification_authority") != "runner"
            or receipt.get("containment_verification_status") != "verified"
            or receipt.get("containment_termination_postcondition")
            != _CONTAINMENT_POSTCONDITION
            or receipt.get("timeout_enforced_locally") is not False
            or receipt.get("timeout_owner") != "runner"
            or not isinstance(containment_ref_sha256, str)
            or containment_ref_sha256 != _sha256(containment_receipt_ref)
        ):
            return None
        compact_receipt.update(
            {
                "containment_contract_validated": True,
                "containment_kind": "container",
                "containment_verification_authority": "runner",
                "containment_verification_status": "verified",
                "containment_termination_postcondition": (
                    _CONTAINMENT_POSTCONDITION
                ),
                "timeout_enforced_locally": False,
                "timeout_owner": "runner",
                "containment_verification_ref_sha256": containment_ref_sha256,
            }
        )
        return {
            "schema_version": EXTERNAL_AGENT_RESULT_SCHEMA_VERSION,
            "status": status,
            "exit_code": exit_code,
            "receipt": compact_receipt,
        }

    @classmethod
    def _read_result(
        cls,
        session: TmuxSession,
        *,
        instruction: str,
        containment_receipt_ref: str,
    ) -> dict[str, object] | None:
        exit_code, output = session._container.exec_run(  # type: ignore[attr-defined]
            ["sh", "-c", f"cat {shlex.quote(_CONTAINER_RESULT_PATH)}"],
            demux=True,
        )
        if int(exit_code) != 0:
            return None
        raw = output[0] if isinstance(output, tuple) else output
        try:
            value = json.loads((raw or b"").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return cls._compact_result(
            value,
            instruction=instruction,
            containment_receipt_ref=containment_receipt_ref,
        )

    @staticmethod
    def _persist_compact_receipt(
        session: TmuxSession, result: Mapping[str, object]
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="loopsbench-external-receipt-") as root:
            receipt_path = Path(root) / "result.public.json"
            receipt_path.write_text(
                json.dumps(dict(result), ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            DockerComposeManager.copy_to_container(
                container=session._container,  # type: ignore[attr-defined]
                paths=receipt_path,
                container_dir=str(Path(_CONTAINER_RECEIPT_PATH).parent),
                container_filename=Path(_CONTAINER_RECEIPT_PATH).name,
            )

    @staticmethod
    def _failure_mode(result: dict[str, object] | None) -> str:
        if result is None:
            return FailureMode.AGENT_STARTUP_ERROR.value
        if result.get("schema_version") != EXTERNAL_AGENT_RESULT_SCHEMA_VERSION:
            return FailureMode.AGENT_STARTUP_ERROR.value
        status = result.get("status")
        if status == "succeeded":
            return FailureMode.NONE.value
        return FailureMode.UNKNOWN_AGENT_ERROR.value

    def perform_task(
        self,
        instruction: str,
        session: TmuxSession,
        logging_dir: Path | None = None,
        timeout_sec: float | None = None,
    ) -> AgentResult:
        del logging_dir
        effective_timeout = timeout_sec or 86_400.0
        container = session._container  # type: ignore[attr-defined]
        generation = _verified_container_generation(container)
        containment_receipt_ref = _containment_receipt_ref(generation)
        request = self._request(
            instruction,
            effective_timeout,
            containment_receipt_ref=containment_receipt_ref,
        )
        with tempfile.TemporaryDirectory(prefix="loopsbench-external-command-") as root:
            request_path = Path(root) / "request.json"
            request_path.write_text(
                json.dumps(request, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            DockerComposeManager.copy_to_container(
                container=session._container,  # type: ignore[attr-defined]
                paths=request_path,
                container_dir=str(Path(_CONTAINER_REQUEST_PATH).parent),
                container_filename="request.json",
            )

        _clear_external_agent_artifacts(container)
        command = shlex.join(self._command)
        environment = (
            f"LOOPSBENCH_EXTERNAL_AGENT_REQUEST={shlex.quote(_CONTAINER_REQUEST_PATH)} "
            f"LOOPSBENCH_EXTERNAL_AGENT_RESULT={shlex.quote(_CONTAINER_RESULT_PATH)} "
            f"LOOPSBENCH_EXTERNAL_AGENT_WORKSPACE=/workspace "
            f"LOOPSBENCH_EXTERNAL_AGENT_TIMEOUT_SECONDS={int(effective_timeout)}"
        )
        timed_out = False
        try:
            session.send_keys(
                f"{environment} {command} Enter",
                block=True,
                max_timeout_sec=effective_timeout,
            )
        except TimeoutError:
            timed_out = True
        finally:
            _restart_and_verify_drained(
                container, previous_generation=generation
            )
            try:
                session.recreate_after_container_restart()
            except Exception as exc:
                _stop_unverified_container(container)
                raise AgentSafetyError(
                    "External command session could not be restored after drain."
                ) from exc
        if timed_out:
            _clear_external_agent_artifacts(container)
            return AgentResult(failure_mode=FailureMode.AGENT_TIMEOUT.value)
        result = self._read_result(
            session,
            instruction=instruction,
            containment_receipt_ref=containment_receipt_ref,
        )
        if result is not None:
            self._persist_compact_receipt(session, result)
        return AgentResult(failure_mode=self._failure_mode(result))
