from __future__ import annotations

import hashlib
import io
import json
import tarfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from loopsbench.agents.base_agent import AgentSafetyError
from loopsbench.agents.external_command_agent import (
    EXTERNAL_AGENT_REQUEST_SCHEMA_VERSION,
    EXTERNAL_AGENT_RESULT_SCHEMA_VERSION,
    ExternalCommandAgent,
    _capture_archive,
    _result_bytes_from_archive,
    public_external_command_metadata,
)
from loopsbench.harness.models import FailureMode

_CONTAINER_ID = "sha256:test-container"
_STARTED_AT = "2026-08-31T00:00:00Z"
_INSTRUCTION = "implement the task"


def _tar_bytes(name: str, value: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(name)
        info.size = len(value)
        archive.addfile(info, io.BytesIO(value))
    return buffer.getvalue()


def _result(
    *,
    launch_binding_digest: str,
    instruction: str = _INSTRUCTION,
    command_argument_count: object = 3,
) -> bytes:
    return json.dumps(
        {
            "schema_version": EXTERNAL_AGENT_RESULT_SCHEMA_VERSION,
            "status": "succeeded",
            "exit_code": 0,
            "receipt": {
                "schema_version": "external_agent_phase_receipt_v2",
                "classification": "solver_completed",
                "command_recorded": False,
                "command_argument_count": command_argument_count,
                "duration_ms": 4,
                "instruction_recorded": False,
                "workspace_recorded": False,
                "instruction_sha256": hashlib.sha256(instruction.encode()).hexdigest(),
                "instruction_chars": len(instruction),
                "launch_binding_digest": launch_binding_digest,
            },
        }
    ).encode()


def _agent_kwargs(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "command": ["runner", "--opaque", "private-marker"],
        "benchmark_id": "loopsbench",
        "case_id": "task-demo",
        "run_id": "run-1",
        "arm_id": "loopx",
        "expected_provider": "trae",
        "expected_model": "GPT-5.4",
        "provider_authority": "runner-profile",
        "integrity_policy_sha256": "a" * 64,
        "resolved_image_id": "sha256:test-image",
    }
    values.update(overrides)
    return values


def _agent(**overrides: object) -> ExternalCommandAgent:
    return ExternalCommandAgent(**_agent_kwargs(**overrides))  # type: ignore[arg-type]


class _Container:
    def __init__(
        self,
        *,
        pause_observed: bool = True,
        mkdir_exit_code: int = 0,
        put_result: bool = True,
        archive_error_path: str | None = None,
    ) -> None:
        self.attrs: dict[str, Any] = {
            "Id": _CONTAINER_ID,
            "Image": "sha256:test-image",
            "Config": {
                "Env": ["PATH=/usr/bin"],
                "Labels": {
                    "com.docker.compose.project": "loopsbench-test",
                    "com.docker.compose.service": "client",
                },
                "Entrypoint": [],
                "Cmd": ["sh", "-c", "sleep infinity"],
                "Healthcheck": {"Test": ["NONE"]},
            },
            "HostConfig": {
                "Privileged": False,
                "PidMode": "",
                "NetworkMode": "loopsbench-test_default",
                "CapAdd": [],
                "Devices": [],
                "SecurityOpt": [],
            },
            "State": {
                "Running": True,
                "Paused": False,
                "StartedAt": _STARTED_AT,
            },
            "Mounts": [],
            "NetworkSettings": {
                "Networks": {
                    "loopsbench-test_default": {
                        "Aliases": ["client"],
                        "NetworkID": "network-id",
                    }
                }
            },
        }
        network = SimpleNamespace(
            attrs={
                "Id": "network-id",
                "Labels": {
                    "com.docker.compose.project": "loopsbench-test",
                    "com.docker.compose.network": "default",
                },
            },
            reload=lambda: None,
        )
        self.client = SimpleNamespace(
            networks=SimpleNamespace(get=lambda _name: network)
        )
        self._loopsbench_expected_compose_project = "loopsbench-test"
        self._loopsbench_expected_compose_service = "client"
        self.pause_observed = pause_observed
        self.mkdir_exit_code = mkdir_exit_code
        self.put_result = put_result
        self.archive_error_path = archive_error_path
        self.events: list[str] = []
        self.request: dict[str, object] | None = None
        self.result: bytes | None = None
        self.workspace_archive = _tar_bytes("workspace/file.txt", b"snapshot")
        self.launch_archive: bytes | None = None
        self.launch_done = False

    def reload(self) -> None:
        self.events.append("reload")

    def pause(self) -> None:
        self.events.append("pause")
        if self.pause_observed:
            self.attrs["State"]["Paused"] = True

    def put_archive(self, _path: str, data: bytes) -> bool:
        self.events.append("put-launch-archive")
        self.launch_archive = data
        if not self.put_result:
            return False
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            request_file = archive.extractfile("request.json")
            assert request_file is not None
            self.request = json.load(request_file)
            admission = self.request["launch_admission"]
            assert isinstance(admission, dict)
            self.result = _result(
                launch_binding_digest=str(admission["launch_binding_digest"])
            )
        return True

    def get_archive(self, path: str):
        if path == self.archive_error_path:
            raise RuntimeError("archive failed")
        if path.endswith("result.json"):
            self.events.append("capture-result")
            assert self.result is not None
            return iter([_tar_bytes("result.json", self.result)]), {}
        if path == "/workspace":
            self.events.append("capture-workspace")
            return iter([self.workspace_archive]), {}
        raise AssertionError(path)

    def exec_run(self, command: list[str], demux: bool = True):
        if command[:2] == ["mkdir", "-p"]:
            self.events.append("mkdir-launch-dir")
            return self.mkdir_exit_code, (b"", b"")
        if command[:2] == ["sh", "-c"]:
            self.events.append("clear-result")
            return 0, (b"", b"")
        if command[:2] == ["rm", "-f"]:
            self.events.append("clear-launch-done")
            self.launch_done = False
            return 0, (b"", b"")
        if command and command[0] == "tmux":
            self.events.append("send-command")
            self.launch_done = True
            return 0, (b"", b"")
        if command[:2] == ["test", "-f"]:
            self.events.append("wait-launch")
            return (0 if self.launch_done else 1), (b"", b"")
        raise AssertionError(command)


class _Session:
    def __init__(self, container: _Container) -> None:
        self._container = container
        self._session_name = "agent-1"
        self.logged_commands: list[str] = []

    def _append_command_log(self, command: str) -> None:
        self.logged_commands.append(command)

    def capture_pane(self, *, capture_entire: bool) -> str:
        self._container.events.append("capture-pane")
        return "final solver pane"


def test_external_command_captures_frozen_state_without_parsing_or_argv_logging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parse_events: list[str] = []
    original_compact = ExternalCommandAgent._compact_result

    def compact_spy(*args: object, **kwargs: object) -> dict[str, object]:
        parse_events.append("parse")
        return original_compact(*args, **kwargs)

    monkeypatch.setattr(ExternalCommandAgent, "_compact_result", compact_spy)
    container = _Container()
    session = _Session(container)
    agent = _agent()

    pending = agent.perform_task(
        _INSTRUCTION, session, logging_dir=tmp_path, timeout_sec=42
    )

    assert pending.failure_mode == FailureMode.NONE.value
    assert parse_events == []
    assert agent.snapshot is not None
    assert agent.snapshot.post_agent_pane == (
        "External solver pane output was not retained by LoopsBench.\n"
    )
    assert agent.snapshot.workspace_archive == container.workspace_archive
    assert container.events[-4:] == [
        "pause",
        "reload",
        "capture-result",
        "capture-workspace",
    ]
    assert session.logged_commands == [
        "/bin/sh /tmp/loopsbench-external-command/launch.sh Enter"
    ]
    assert "private-marker" not in session.logged_commands[0]
    assert not (tmp_path / "trajectories" / "result.public.json").exists()
    assert container.request is not None
    assert container.request["schema_version"] == EXTERNAL_AGENT_REQUEST_SCHEMA_VERSION


def test_external_command_finalizes_only_from_captured_raw_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = _Container()
    agent = _agent()
    agent.perform_task(_INSTRUCTION, _Session(container), logging_dir=tmp_path)
    snapshot = agent.snapshot
    assert snapshot is not None
    captured_result = snapshot.raw_result_archive
    assert captured_result is not None
    container.result = b"mutated-after-pause"
    monkeypatch.setattr(
        container,
        "get_archive",
        lambda _path: pytest.fail("finalize must not read the live container"),
    )

    result = agent.finalize_snapshot(snapshot)

    assert result.failure_mode == FailureMode.NONE.value
    public_result = json.loads(
        (tmp_path / "trajectories" / "result.public.json").read_text()
    )
    assert public_result["status"] == "succeeded"
    assert public_result["receipt"]["schema_version"] == (
        "external_agent_phase_receipt_v2"
    )
    assert not any(key.startswith("containment_") for key in public_result["receipt"])


def test_external_command_keeps_containment_pending_until_runner_owned_proof(
    tmp_path: Path,
) -> None:
    container = _Container()
    agent = _agent()
    agent.perform_task(_INSTRUCTION, _Session(container), logging_dir=tmp_path)
    snapshot = agent.snapshot
    assert snapshot is not None
    assert container.request is not None
    containment = container.request["containment"]
    assert isinstance(containment, dict)
    assert containment["termination_postcondition"] == (
        "destroyed_before_result_consumption"
    )
    verification = containment["verification"]
    assert isinstance(verification, dict)
    assert verification == {
        "schema_version": "external_agent_containment_verification_v1",
        "status": "pending",
        "authority": "runner",
        "receipt_ref": snapshot.containment_receipt_ref,
    }
    proof_path = tmp_path / "trajectories" / "containment.public.json"
    assert not proof_path.exists()

    agent.persist_containment_absence_proof(
        snapshot, container_name="private-container-name"
    )

    proof = json.loads(proof_path.read_text())
    assert proof["schema_version"] == ("loopsbench_external_agent_containment_proof_v1")
    assert proof["containment_absence_verified"] is True
    assert proof["containment_termination_postcondition"] == (
        "destroyed_before_result_consumption"
    )
    assert "private-container-name" not in proof_path.read_text()
    assert _CONTAINER_ID not in proof_path.read_text()


@pytest.mark.parametrize(
    ("container_kwargs", "snapshot_error"),
    [
        ({"pause_observed": False}, "external_agent_snapshot_pause_unverified"),
        (
            {"archive_error_path": "/tmp/loopsbench-external-command/result.json"},
            "external_agent_result_capture_failed",
        ),
        (
            {"archive_error_path": "/workspace"},
            "external_agent_workspace_capture_failed",
        ),
    ],
)
def test_external_command_snapshot_failures_are_typed_and_unparsed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    container_kwargs: dict[str, object],
    snapshot_error: str,
) -> None:
    monkeypatch.setattr(
        ExternalCommandAgent,
        "_compact_result",
        lambda *_args, **_kwargs: pytest.fail("snapshot failure must not parse"),
    )
    agent = _agent()

    agent.perform_task(
        _INSTRUCTION,
        _Session(_Container(**container_kwargs)),  # type: ignore[arg-type]
        logging_dir=tmp_path,
    )

    assert agent.snapshot is not None
    assert str(agent.snapshot.snapshot_error) == snapshot_error


@pytest.mark.parametrize(
    "container",
    [_Container(mkdir_exit_code=1), _Container(put_result=False)],
)
def test_external_command_launch_delivery_fails_closed(
    tmp_path: Path, container: _Container
) -> None:
    with pytest.raises(AgentSafetyError, match="launch_admission_delivery_failed"):
        _agent().perform_task(_INSTRUCTION, _Session(container), logging_dir=tmp_path)

    assert "send-command" not in container.events


def test_external_command_requires_host_logging_before_container_access() -> None:
    container = _Container()
    with pytest.raises(AgentSafetyError, match="external_agent_receipt_persist_failed"):
        _agent().perform_task(_INSTRUCTION, _Session(container))
    assert container.events == []


def test_external_command_rejects_tampered_captured_result(tmp_path: Path) -> None:
    container = _Container()
    agent = _agent()
    agent.perform_task(_INSTRUCTION, _Session(container), logging_dir=tmp_path)
    snapshot = agent.snapshot
    assert snapshot is not None
    tampered = json.loads(
        tarfile.open(fileobj=io.BytesIO(snapshot.raw_result_archive))
        .extractfile("result.json")
        .read()
    )
    tampered["receipt"]["instruction_chars"] += 1
    bad_snapshot = replace(
        snapshot,
        raw_result_archive=_tar_bytes("result.json", json.dumps(tampered).encode()),
    )
    with pytest.raises(
        AgentSafetyError, match="external_agent_result_binding_mismatch"
    ):
        agent.finalize_snapshot(bad_snapshot)


@pytest.mark.parametrize(
    "command",
    [
        ["runner", "--api-key=private-value"],
        ["runner", "--api-key", "private-value"],
        ["runner", "--access-token-file", "/tmp/key"],
        ["curl", "-u", "alice:private-value"],
        ["curl", "--user", "alice:private-value"],
        ["curl", "--oauth2-bearer", "private-value"],
        ["curl", "--header", "Cookie: session=private-value"],
        ["curl", "--key", "private-key.pem"],
        [
            "env",
            "curl",
            "-u",
            "alice:private-value",
            "https://example.test",
        ],
        [
            "busybox",
            "curl",
            "--user",
            "alice:private-value",
            "https://example.test",
        ],
        [
            "timeout",
            "30",
            "curl",
            "--oauth2-bearer",
            "private-value",
            "https://example.test",
        ],
        ["runner", "header=X-Api-Key: private-value"],
        ["runner", "PASSWORD=private-value"],
        ["runner", "https://user:private-value@example.test/path"],
        ["runner", '{"client_secret":"private-value"}'],
        [
            "loopx",
            "benchmark",
            "agent-phase",
            "--solver-command-json",
            '["curl","-u","alice:private-value"]',
        ],
    ],
)
def test_external_command_rejects_inline_credentials_without_echoing_value(
    command: list[str],
) -> None:
    kwargs = _agent_kwargs(command=command)

    with pytest.raises(AgentSafetyError) as preseed_error:
        ExternalCommandAgent.validate_preseed(kwargs)
    with pytest.raises(AgentSafetyError) as constructor_error:
        ExternalCommandAgent(**kwargs)  # type: ignore[arg-type]

    assert str(preseed_error.value) == "credential_isolation_unavailable"
    assert str(constructor_error.value) == "credential_isolation_unavailable"
    assert "private-value" not in str(preseed_error.value)
    assert "private-value" not in str(constructor_error.value)


@pytest.mark.parametrize(
    "command",
    [
        ["sh", "-c", "curl -u alice:private-value https://example.test"],
        ["/bin/bash", "-ec", "runner --mode task"],
        ["zsh", "-fc", "runner --mode task"],
        ["dash", "-c", "runner --mode task"],
        ["ash", "-c", "runner --mode task"],
        ["ksh", "-xc", "runner --mode task"],
        ["/usr/bin/env", "sh", "-c", "runner --mode task"],
        ["busybox", "sh", "-c", "runner --mode task"],
    ],
)
def test_external_command_rejects_shell_command_strings_without_echoing_value(
    command: list[str],
) -> None:
    kwargs = _agent_kwargs(command=command)

    with pytest.raises(AgentSafetyError) as preseed_error:
        ExternalCommandAgent.validate_preseed(kwargs)
    with pytest.raises(AgentSafetyError) as constructor_error:
        ExternalCommandAgent(**kwargs)  # type: ignore[arg-type]

    assert str(preseed_error.value) == "external_command_shell_string_unavailable"
    assert str(constructor_error.value) == ("external_command_shell_string_unavailable")
    command_body = command[-1]
    assert command_body not in str(preseed_error.value)
    assert command_body not in str(constructor_error.value)


@pytest.mark.parametrize(
    "command",
    [
        ["env", "-S", "solver --mode task"],
        ["env", "-iS", "solver --mode task"],
        ["env", "--split-string", "solver --mode task"],
        ["env", "--split-string=solver --mode task"],
        [
            "loopx",
            "benchmark",
            "agent-phase",
            "--solver-command-json",
            '["env","-S","solver --mode task"]',
        ],
    ],
)
def test_external_command_rejects_env_split_string_without_echoing_value(
    command: list[str],
) -> None:
    kwargs = _agent_kwargs(command=command)

    with pytest.raises(AgentSafetyError) as preseed_error:
        ExternalCommandAgent.validate_preseed(kwargs)
    with pytest.raises(AgentSafetyError) as constructor_error:
        ExternalCommandAgent(**kwargs)  # type: ignore[arg-type]

    assert str(preseed_error.value) == "external_command_env_split_string_unavailable"
    assert (
        str(constructor_error.value) == "external_command_env_split_string_unavailable"
    )
    assert "solver --mode task" not in str(preseed_error.value)
    assert "solver --mode task" not in str(constructor_error.value)


@pytest.mark.parametrize(
    "command",
    [
        ["runner", "--model", "token-classifier"],
        ["python", "-u", "solver.py"],
        ["sh", "script.sh", "--mode", "task"],
        ["sh", "--", "solver.sh", "-c"],
        ["curl", "--user-agent", "loopsbench"],
        ["curl", "--header", "Accept: application/json"],
        ["curl", "--header", "User-Agent: loopsbench"],
        ["env", "curl", "--header", "Accept: application/json"],
        ["busybox", "curl", "--user-agent", "loopsbench"],
        ["timeout", "30", "curl", "https://example.test"],
        [
            "loopx",
            "benchmark",
            "agent-phase",
            "--solver-command-json",
            '["solver","--mode","task"]',
        ],
    ],
)
def test_external_command_allows_noncredential_argv(command: list[str]) -> None:
    kwargs = _agent_kwargs(command=command)

    ExternalCommandAgent.validate_preseed(kwargs)

    assert ExternalCommandAgent(**kwargs)._command == command  # type: ignore[arg-type]


def test_external_command_allows_env_var_assignment_without_split_string() -> None:
    kwargs = _agent_kwargs(command=["env", "LANG=C", "solver", "--mode", "task"])

    ExternalCommandAgent.validate_preseed(kwargs)

    assert ExternalCommandAgent(**kwargs)._command == [  # type: ignore[arg-type]
        "env",
        "LANG=C",
        "solver",
        "--mode",
        "task",
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_provider", "sk-live-secret"),
        ("expected_model", "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"),
    ],
)
def test_external_command_rejects_credential_shaped_public_metadata(
    field: str, value: str
) -> None:
    kwargs = _agent_kwargs(**{field: value})

    with pytest.raises(AgentSafetyError) as preseed_error:
        ExternalCommandAgent.validate_preseed(kwargs)
    with pytest.raises(AgentSafetyError) as constructor_error:
        ExternalCommandAgent(**kwargs)  # type: ignore[arg-type]

    assert str(preseed_error.value) == "credential_isolation_unavailable"
    assert str(constructor_error.value) == "credential_isolation_unavailable"
    assert value not in str(preseed_error.value)
    assert value not in str(constructor_error.value)


@pytest.mark.parametrize(
    ("field", "value", "prefix"),
    [
        (
            "run_id",
            "fixtureopaqueABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
            "public:run:sha256:",
        ),
        (
            "case_id",
            "fixtureopaqueabcdefghijklmnopqrstuvwxyz0123456789/case-1",
            "public:case:sha256:",
        ),
        (
            "provider_authority",
            "fixtureopaque-abcdefghijklmnopqrstuvwxyz0123456789",
            "public:authority:sha256:",
        ),
    ],
)
def test_external_command_digests_opaque_identity_metadata(
    field: str, value: str, prefix: str
) -> None:
    kwargs = _agent_kwargs(**{field: value})

    ExternalCommandAgent.validate_preseed(kwargs)
    agent = ExternalCommandAgent(**kwargs)  # type: ignore[arg-type]

    assert str(agent._launch_identity[field]).startswith(prefix)
    assert value not in str(agent._launch_identity[field])


def test_external_command_allows_public_metadata_with_noncredential_words() -> None:
    kwargs = _agent_kwargs(
        run_id="run-ghp-demo",
        expected_provider="github-provider",
        expected_model="sk-projection-model",
        provider_authority="runner-profile",
    )

    ExternalCommandAgent.validate_preseed(kwargs)
    agent = ExternalCommandAgent(**kwargs)  # type: ignore[arg-type]

    assert agent._launch_identity["run_id"] == "run-ghp-demo"
    assert agent._launch_identity["expected_provider"] == "github-provider"
    assert agent._launch_identity["expected_model"] == "sk-projection-model"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_provider", "fixtureopaqueabcdefghijklmnopqrstuvwxyz"),
        ("expected_model", "fixture-opaque-abcdefghijklmnopqrstuvwxyz"),
        (
            "expected_model",
            "aaaaaaaa.bbbbbbbb.cccccccc",
        ),
        ("expected_provider", "fixtureopaqueABCDEFGHIJKLMNOP"),
    ],
)
def test_external_command_rejects_opaque_route_labels(field: str, value: str) -> None:
    kwargs = _agent_kwargs(**{field: value})

    with pytest.raises(AgentSafetyError) as preseed_error:
        ExternalCommandAgent.validate_preseed(kwargs)
    with pytest.raises(AgentSafetyError) as constructor_error:
        ExternalCommandAgent(**kwargs)  # type: ignore[arg-type]

    assert str(preseed_error.value) == "credential_isolation_unavailable"
    assert str(constructor_error.value) == "credential_isolation_unavailable"
    assert value not in str(preseed_error.value)
    assert value not in str(constructor_error.value)


def test_external_command_rejects_malformed_nested_solver_argv() -> None:
    with pytest.raises(AgentSafetyError) as exc_info:
        _agent(
            command=[
                "loopx",
                "benchmark",
                "agent-phase",
                "--solver-command-json",
                "not-json",
            ]
        )

    assert str(exc_info.value) == "credential_isolation_unavailable"


def test_external_command_rejects_nested_shell_command_string() -> None:
    with pytest.raises(AgentSafetyError) as exc_info:
        _agent(
            command=[
                "loopx",
                "benchmark",
                "agent-phase",
                "--solver-command-json",
                '["sh","-c","runner --mode task"]',
            ]
        )

    assert str(exc_info.value) == "external_command_shell_string_unavailable"


def test_public_external_command_metadata_preserves_outer_bridge_count_only() -> None:
    metadata = public_external_command_metadata(
        [
            "loopx",
            "benchmark",
            "agent-phase",
            "--solver-command-json",
            '["solver","--mode","task"]',
        ]
    )

    assert metadata["command_recorded"] is False
    assert metadata["command_argument_count"] == 5
    assert isinstance(metadata["command_sha256"], str)


@pytest.mark.parametrize(
    "nested_command",
    [
        json.dumps([[[[[["solver"]]]]]]),
        json.dumps(["argument"] * 300),
    ],
)
def test_external_command_rejects_nested_solver_argv_beyond_scan_budget(
    nested_command: str,
) -> None:
    with pytest.raises(AgentSafetyError) as exc_info:
        _agent(
            command=[
                "loopx",
                "benchmark",
                "agent-phase",
                "--solver-command-json",
                nested_command,
            ]
        )

    assert str(exc_info.value) == "credential_isolation_unavailable"


def test_external_command_retains_nested_solver_command_count() -> None:
    launch_binding_digest = "b" * 64
    value = json.loads(
        _result(launch_binding_digest=launch_binding_digest, command_argument_count=7)
    )

    result = ExternalCommandAgent._compact_result(
        value,
        instruction=_INSTRUCTION,
        launch_binding_digest=launch_binding_digest,
        committed_command_argument_count=7,
        duration_ms=12,
    )

    assert result["receipt"]["command_argument_count"] == 7
    assert result["receipt"]["duration_ms"] == 12
    assert set(result["receipt"]) == {
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


@pytest.mark.parametrize("command_argument_count", [0, -1, True, False])
def test_external_command_rejects_invalid_nested_solver_command_count(
    command_argument_count: object,
) -> None:
    launch_binding_digest = "b" * 64
    value = json.loads(
        _result(
            launch_binding_digest=launch_binding_digest,
            command_argument_count=command_argument_count,
        )
    )

    with pytest.raises(AgentSafetyError, match="external_agent_result_invalid"):
        ExternalCommandAgent._compact_result(
            value,
            instruction=_INSTRUCTION,
            launch_binding_digest=launch_binding_digest,
            committed_command_argument_count=3,
            duration_ms=12,
        )


def test_external_command_rejects_nested_solver_command_count_tamper() -> None:
    launch_binding_digest = "b" * 64
    value = json.loads(
        _result(launch_binding_digest=launch_binding_digest, command_argument_count=999)
    )

    with pytest.raises(
        AgentSafetyError, match="external_agent_result_binding_mismatch"
    ):
        ExternalCommandAgent._compact_result(
            value,
            instruction=_INSTRUCTION,
            launch_binding_digest=launch_binding_digest,
            committed_command_argument_count=7,
            duration_ms=12,
        )


def test_external_command_overwrites_solver_duration_with_runner_observation(
    tmp_path: Path,
) -> None:
    container = _Container()
    agent = _agent()
    agent.perform_task(_INSTRUCTION, _Session(container), logging_dir=tmp_path)
    snapshot = agent.snapshot
    assert snapshot is not None

    agent.finalize_snapshot(replace(snapshot, duration_ms=9876))

    result = json.loads((tmp_path / "trajectories" / "result.public.json").read_text())
    assert result["receipt"]["duration_ms"] == 9876


def test_external_command_archive_capture_stops_at_stream_limit() -> None:
    observed = 0

    def chunks():
        nonlocal observed
        for _ in range(3):
            observed += 1
            yield b"1234"

    container = SimpleNamespace(get_archive=lambda _path: (chunks(), {}))
    with pytest.raises(
        AgentSafetyError, match="external_agent_result_archive_stream_limit_exceeded"
    ):
        _capture_archive(
            container,
            "/result",
            "external_agent_result_capture_failed",
            kind="result",
            max_stream_bytes=5,
            max_expanded_bytes=100,
            max_members=2,
            max_member_bytes=100,
        )
    assert observed == 2


def test_result_archive_rejects_oversized_declared_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "loopsbench.agents.external_command_agent._MAX_RESULT_JSON_BYTES", 8
    )
    with pytest.raises(AgentSafetyError, match="member_limit_exceeded"):
        _result_bytes_from_archive(_tar_bytes("result.json", b"123456789"))
