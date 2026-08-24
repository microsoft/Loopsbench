from __future__ import annotations

import hashlib
import json
from pathlib import Path

from loopsbench.agents.external_command_agent import (
    EXTERNAL_AGENT_REQUEST_SCHEMA_VERSION,
    EXTERNAL_AGENT_RESULT_SCHEMA_VERSION,
    ExternalCommandAgent,
)
from loopsbench.harness.models import FailureMode


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
            },
        }
    ).encode()


class _Container:
    def __init__(self, result: bytes | None):
        self.result = result

    def exec_run(self, _command, demux=True):
        if self.result is None:
            return 1, (b"", b"missing")
        return 0, (self.result, b"")


class _Session:
    def __init__(self, result: bytes | None):
        self._container = _Container(result)
        self.commands: list[tuple[str, bool, float]] = []

    def send_keys(self, command: str, block: bool, max_timeout_sec: float) -> None:
        self.commands.append((command, block, max_timeout_sec))


def test_external_command_writes_request_and_maps_success(
    tmp_path: Path, monkeypatch
) -> None:
    copied: list[tuple[str, dict[str, object]]] = []

    def copy_to_container(*, container, paths, container_dir, container_filename):
        copied.append(
            (
                container_dir,
                json.loads(Path(paths).read_text(encoding="utf-8")),
            )
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
    assert copied[0] == (
        "/tmp/loopsbench-external-command",
        {
        "schema_version": EXTERNAL_AGENT_REQUEST_SCHEMA_VERSION,
        "instruction": "implement the task",
        "workspace": "/workspace",
        "timeout_seconds": 42,
        },
    )
    assert copied[1][0] == "/agent-logs/external-command"
    assert copied[1][1]["receipt"]["instruction_recorded"] is False
    command, block, timeout = session.commands[0]
    assert "runner --flag" in command
    assert (
        "LOOPSBENCH_EXTERNAL_AGENT_REQUEST=/tmp/loopsbench-external-command/request.json"
        in command
    )
    assert block is True
    assert timeout == 42


def test_external_command_maps_missing_result_to_startup_error(monkeypatch) -> None:
    monkeypatch.setattr(
        "loopsbench.agents.external_command_agent.DockerComposeManager.copy_to_container",
        lambda **_kwargs: None,
    )
    result = ExternalCommandAgent(command=["runner"]).perform_task(
        "implement the task",
        _Session(None),  # type: ignore[arg-type]
    )

    assert result.failure_mode == FailureMode.AGENT_STARTUP_ERROR.value


def test_external_command_rejects_mismatched_instruction_hash(monkeypatch) -> None:
    monkeypatch.setattr(
        "loopsbench.agents.external_command_agent.DockerComposeManager.copy_to_container",
        lambda **_kwargs: None,
    )
    result = ExternalCommandAgent(command=["runner"]).perform_task(
        "implement the task",
        _Session(_result(instruction="different task")),  # type: ignore[arg-type]
    )

    assert result.failure_mode == FailureMode.AGENT_STARTUP_ERROR.value


def test_external_command_rejects_unbounded_result_shape(monkeypatch) -> None:
    monkeypatch.setattr(
        "loopsbench.agents.external_command_agent.DockerComposeManager.copy_to_container",
        lambda **_kwargs: None,
    )
    result = ExternalCommandAgent(command=["runner"]).perform_task(
        "implement the task",
        _Session(
            json.dumps(
                {
                    "schema_version": EXTERNAL_AGENT_RESULT_SCHEMA_VERSION,
                    "status": "succeeded",
                    "exit_code": 0,
                    "receipt": {
                        "schema_version": "loopx_external_agent_phase_receipt_v1",
                        "classification": "solver_completed",
                        "command_recorded": False,
                        "command_argument_count": 1,
                        "duration_ms": 1,
                        "instruction_recorded": False,
                        "workspace_recorded": False,
                        "raw_instruction": "do not export this",
                    },
                }
            ).encode()
        ),  # type: ignore[arg-type]
    )

    assert result.failure_mode == FailureMode.AGENT_STARTUP_ERROR.value


def test_external_command_rejects_non_argv_command() -> None:
    try:
        ExternalCommandAgent(command=["runner", 3])  # type: ignore[list-item]
    except ValueError as error:
        assert str(error) == "ExternalCommandAgent requires a non-empty command argv."
    else:
        raise AssertionError("invalid argv must fail")
