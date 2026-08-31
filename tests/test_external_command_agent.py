from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopsbench.agents.base_agent import AgentSafetyError
from loopsbench.agents.external_command_agent import (
    EXTERNAL_AGENT_CONTAINMENT_SCHEMA_VERSION,
    EXTERNAL_AGENT_CONTAINMENT_VERIFICATION_SCHEMA_VERSION,
    EXTERNAL_AGENT_REQUEST_SCHEMA_VERSION,
    EXTERNAL_AGENT_RESULT_SCHEMA_VERSION,
    ExternalCommandAgent,
)
from loopsbench.harness.harness import Harness
from loopsbench.harness.models import FailureMode
from loopsbench.terminal.tmux_session import TmuxSession

_CONTAINER_ID = "sha256:test-container"
_INITIAL_STARTED_AT = "2026-08-31T00:00:00Z"
_RESTARTED_AT = "2026-08-31T00:00:01Z"
_CONTAINMENT_RECEIPT_REF = (
    "loopsbench-container:"
    + hashlib.sha256(
        f"{_CONTAINER_ID}\0{_INITIAL_STARTED_AT}".encode()
    ).hexdigest()[:24]
)


def _result(
    status: str = "succeeded", *, instruction: str = "implement the task"
) -> bytes:
    return json.dumps(
        {
            "schema_version": EXTERNAL_AGENT_RESULT_SCHEMA_VERSION,
            "status": status,
            "exit_code": 0 if status == "succeeded" else 1,
            "receipt": {
                "schema_version": "loopx_external_agent_phase_receipt_v1",
                "classification": "solver_completed",
                "command_recorded": False,
                "command_argument_count": 2,
                "duration_ms": 4,
                "instruction_recorded": False,
                "workspace_recorded": False,
                "instruction_sha256": hashlib.sha256(
                    instruction.encode("utf-8")
                ).hexdigest(),
                "instruction_chars": len(instruction),
                "containment_contract_validated": True,
                "containment_kind": "container",
                "containment_verification_authority": "runner",
                "containment_verification_status": "verified",
                "containment_termination_postcondition": (
                    "drained_before_result_consumption"
                ),
                "timeout_enforced_locally": False,
                "timeout_owner": "runner",
                "containment_verification_ref_sha256": hashlib.sha256(
                    _CONTAINMENT_RECEIPT_REF.encode()
                ).hexdigest(),
            },
        }
    ).encode()


class _Container:
    def __init__(
        self,
        result: bytes | None,
        *,
        privileged: bool = False,
        pid_mode: str = "",
        mounts: list[dict[str, object]] | None = None,
        restart_changes_generation: bool = True,
    ):
        self.result = result
        self.attrs: dict[str, object] = {
            "Id": _CONTAINER_ID,
            "HostConfig": {"Privileged": privileged, "PidMode": pid_mode},
            "State": {"Running": True, "StartedAt": _INITIAL_STARTED_AT},
            "Mounts": mounts or [],
        }
        self.restart_changes_generation = restart_changes_generation
        self.events: list[str] = []

    def reload(self) -> None:
        self.events.append("reload")

    def restart(self, timeout: int) -> None:
        self.events.append(f"restart:{timeout}")
        if self.restart_changes_generation:
            self.attrs["State"]["StartedAt"] = _RESTARTED_AT  # type: ignore[index]

    def kill(self) -> None:
        self.events.append("kill")

    def exec_run(self, command, demux=True):
        command_text = " ".join(command) if isinstance(command, list) else str(command)
        if "rm -f --" in command_text:
            self.events.append("clear-artifacts")
            return 0, (b"", b"")
        self.events.append("read-result")
        if self.result is None:
            return 1, (b"", b"missing")
        return 0, (self.result, b"")


class _Session:
    def __init__(
        self,
        result: bytes | None,
        *,
        container: _Container | None = None,
        timeout: bool = False,
    ):
        self._container = container or _Container(result)
        self.commands: list[tuple[str, bool, float]] = []
        self.timeout = timeout

    def recreate_after_container_restart(self) -> None:
        self._container.events.append("recreate-session")

    def send_keys(self, command: str, block: bool, max_timeout_sec: float) -> None:
        self.commands.append((command, block, max_timeout_sec))
        self._container.events.append("send-command")
        if self.timeout:
            raise TimeoutError("agent timed out")


def _disable_receipt_copy(monkeypatch) -> None:
    monkeypatch.setattr(
        "loopsbench.agents.external_command_agent.DockerComposeManager.copy_to_container",
        lambda **_kwargs: None,
    )


def test_external_command_writes_contained_request_and_drains_before_read(
    tmp_path: Path, monkeypatch
) -> None:
    copied: list[tuple[str, dict[str, object]]] = []

    def copy_to_container(*, container, paths, container_dir, container_filename):
        copied.append(
            (container_dir, json.loads(Path(paths).read_text(encoding="utf-8")))
        )

    monkeypatch.setattr(
        "loopsbench.agents.external_command_agent.DockerComposeManager.copy_to_container",
        copy_to_container,
    )
    session = _Session(_result())
    result = ExternalCommandAgent(command=["runner", "--flag"]).perform_task(
        "implement the task",
        session,  # type: ignore[arg-type]
        logging_dir=tmp_path,
        timeout_sec=42,
    )

    assert result.failure_mode == FailureMode.NONE.value
    request = copied[0][1]
    assert request["schema_version"] == EXTERNAL_AGENT_REQUEST_SCHEMA_VERSION
    assert request["containment"] == {
        "schema_version": EXTERNAL_AGENT_CONTAINMENT_SCHEMA_VERSION,
        "kind": "container",
        "timeout_owner": "runner",
        "termination_postcondition": "drained_before_result_consumption",
        "verification": {
            "schema_version": (
                EXTERNAL_AGENT_CONTAINMENT_VERIFICATION_SCHEMA_VERSION
            ),
            "status": "verified",
            "authority": "runner",
            "receipt_ref": _CONTAINMENT_RECEIPT_REF,
        },
    }
    assert session._container.events == [
        "reload",
        "clear-artifacts",
        "send-command",
        "restart:0",
        "reload",
        "recreate-session",
        "read-result",
    ]


def test_external_command_timeout_drains_before_returning(monkeypatch) -> None:
    _disable_receipt_copy(monkeypatch)
    container = _Container(_result())
    result = ExternalCommandAgent(command=["runner"]).perform_task(
        "implement the task",
        _Session(_result(), container=container, timeout=True),  # type: ignore[arg-type]
        timeout_sec=1,
    )

    assert result.failure_mode == FailureMode.AGENT_TIMEOUT.value
    assert container.events[-4:] == [
        "restart:0",
        "reload",
        "recreate-session",
        "clear-artifacts",
    ]


@pytest.mark.parametrize(
    "container",
    [
        _Container(None, privileged=True),
        _Container(None, pid_mode="host"),
        _Container(
            None,
            mounts=[
                {
                    "Source": "/var/run/docker.sock",
                    "Destination": "/var/run/docker.sock",
                }
            ],
        ),
    ],
)
def test_external_command_rejects_unsafe_container_before_launch(
    monkeypatch, container: _Container
) -> None:
    _disable_receipt_copy(monkeypatch)
    session = _Session(None, container=container)
    with pytest.raises(AgentSafetyError):
        ExternalCommandAgent(command=["runner"]).perform_task(
            "implement the task", session  # type: ignore[arg-type]
        )
    assert session.commands == []


def test_external_command_fails_closed_when_drain_is_not_observed(
    monkeypatch,
) -> None:
    _disable_receipt_copy(monkeypatch)
    container = _Container(_result(), restart_changes_generation=False)
    with pytest.raises(AgentSafetyError, match="generation did not change"):
        ExternalCommandAgent(command=["runner"]).perform_task(
            "implement the task",
            _Session(_result(), container=container),  # type: ignore[arg-type]
        )
    assert container.events[-1] == "kill"


def test_external_command_rejects_invalid_result_contract(monkeypatch) -> None:
    _disable_receipt_copy(monkeypatch)
    payload = json.loads(_result())
    payload["receipt"]["instruction_chars"] += 1
    result = ExternalCommandAgent(command=["runner"]).perform_task(
        "implement the task",
        _Session(json.dumps(payload).encode()),  # type: ignore[arg-type]
    )
    assert result.failure_mode == FailureMode.AGENT_STARTUP_ERROR.value


def test_harness_propagates_agent_safety_error_before_verification(
    tmp_path: Path,
) -> None:
    class _UnsafeAgent:
        def perform_task(self, **_kwargs):
            raise AgentSafetyError("containment not drained")

    harness = object.__new__(Harness)
    with pytest.raises(AgentSafetyError, match="containment not drained"):
        harness._run_agent(
            session=None,
            handler=SimpleNamespace(task=SimpleNamespace(max_agent_timeout_sec=60)),
            agent=_UnsafeAgent(),  # type: ignore[arg-type]
            instruction="implement the task",
            logging_dir=tmp_path,
            timeout_sec=1,
        )


def test_tmux_session_creation_failure_is_not_silent() -> None:
    class _FailingContainer:
        def exec_run(self, _command, demux=True):
            return 1, (b"", b"tmux unavailable")

    session = object.__new__(TmuxSession)
    session._container = _FailingContainer()
    session._session_name = "agent-after-drain"
    with pytest.raises(RuntimeError, match="tmux unavailable"):
        session.recreate_after_container_restart()


def test_external_command_rejects_non_argv_command() -> None:
    with pytest.raises(ValueError, match="non-empty command argv"):
        ExternalCommandAgent(command=["runner", 3])  # type: ignore[list-item]
