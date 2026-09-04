from __future__ import annotations

import hashlib
import io
import json
import logging
import tarfile
from contextlib import AbstractContextManager
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopsbench.agents.agent_name import AgentName
from loopsbench.agents.base_agent import AgentResult, AgentSafetyError
from loopsbench.agents.external_command_agent import (
    ExternalCommandAgent,
    ExternalCommandSnapshot,
)
from loopsbench.handlers.trial_handler import TrialHandler
from loopsbench.harness.harness import Harness
from loopsbench.harness.models import FailureMode
from loopsbench.parsers.base_parser import UnitTestStatus
from loopsbench.task_images.strategy import DockerImageStrategy
from loopsbench.terminal.docker_compose_manager import (
    ContainerAdmissionError,
    DockerImageResolution,
)


def _workspace_archive() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        value = b"snapshot"
        info = tarfile.TarInfo("workspace/file.txt")
        info.size = len(value)
        archive.addfile(info, io.BytesIO(value))
    return buffer.getvalue()


class _SafetyResultAgent:
    def perform_task(self, **_kwargs: object) -> AgentResult:
        return AgentResult(failure_mode=FailureMode.AGENT_SAFETY_ERROR.value)

    def get_trajectory_paths(self) -> list[str]:
        return []


class _RaisingSafetyAgent:
    def perform_task(self, **_kwargs: object) -> AgentResult:
        raise AgentSafetyError("external_agent_execution_failed")

    def get_trajectory_paths(self) -> list[str]:
        return []


class _Session:
    def capture_pane(self, *, capture_entire: bool) -> str:
        return ""


class _Terminal(AbstractContextManager["_Terminal"]):
    tester_container = None

    def __init__(self) -> None:
        self.compose_manager = SimpleNamespace(
            image_resolution=DockerImageResolution(
                requested_client_image_ref="loopsbench__task_compiler__client",
                resolved_image_id="sha256:test-image",
            )
        )

    def create_session(self, name: str, **_kwargs: object) -> _Session:
        assert name == "agent-1"
        return _Session()

    def __enter__(self) -> _Terminal:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class _SnapshotTester:
    def __init__(
        self, *, cleanup_exit_code: int = 0, transfer_result: bool = True
    ) -> None:
        self.cleanup_exit_code = cleanup_exit_code
        self.transfer_result = transfer_result
        self.received: bytes | None = None
        self.id = "tester-id"

    def exec_run(self, _command: object):
        return self.cleanup_exit_code, b""

    def put_archive(self, _path: str, archive: bytes) -> bool:
        self.received = archive
        return self.transfer_result


def _external_terminal(tester: _SnapshotTester) -> SimpleNamespace:
    evidence = SimpleNamespace(
        container_id=tester.id,
        started_at="2026-09-03T00:00:00Z",
        image_id="sha256:test-image",
    )
    return SimpleNamespace(
        tester_container=tester,
        compose_manager=SimpleNamespace(validate_live_tester=lambda: evidence),
    )


def _make_task(tmp_path: Path) -> Path:
    task_dir = tmp_path / "tasks" / "task_compiler"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        "instruction: test instruction\nrun_tests_in_same_shell: true\n"
    )
    (task_dir / "docker-compose.yaml").write_text("services: {}\n")
    return task_dir


def _make_harness(tmp_path: Path, agent_name: AgentName) -> Harness:
    harness = Harness.__new__(Harness)
    harness._logger = logging.getLogger("test")
    harness._output_path = tmp_path / "runs"
    harness._run_id = "test_run"
    harness._agent_name = agent_name
    harness._model_name = "GPT-5.4"
    harness._agent_kwargs = {}
    harness._global_agent_timeout_sec = None
    harness._global_test_timeout_sec = None
    harness._global_timeout_multiplier = 1.0
    harness._docker_image_strategy = DockerImageStrategy.LOCAL_EXISTING
    harness._docker_image_namespace = None
    harness._docker_image_tag = None
    harness._no_rebuild = True
    harness._cleanup = True
    harness._rate_limit_retries_per_round = 0
    harness._rate_limit_backoff_sec = 0.0
    return harness


def _construct_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent_kwargs: dict[str, object],
    n_attempts: int = 1,
) -> Harness:
    monkeypatch.setattr(
        Harness,
        "_init_dataset",
        lambda self: setattr(
            self, "_dataset", SimpleNamespace(sort_by_duration=lambda: None)
        ),
    )
    monkeypatch.setattr(Harness, "_init_agent_class", lambda self: None)
    monkeypatch.setattr(Harness, "_init_logger", lambda self: None)
    return Harness(
        output_path=tmp_path / "runs",
        run_id="test_run",
        agent_name=AgentName.EXTERNAL_COMMAND,
        model_name="GPT-5.4",
        agent_kwargs=agent_kwargs,
        n_attempts=n_attempts,
        docker_image_strategy=DockerImageStrategy.LOCAL_EXISTING,
    )


@pytest.mark.parametrize(
    ("agent_kwargs", "error"),
    [
        ({"outer_loop_count": 2}, "outer_loop_count=1"),
        ({"outer_loop_count": "unlimited"}, "outer_loop_count=1"),
        (
            {"rate_limit_retries_per_round": 1},
            "rate_limit_retries_per_round=0",
        ),
    ],
)
def test_external_command_rejects_result_conditioned_retry_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_kwargs: dict[str, object],
    error: str,
) -> None:
    monkeypatch.delenv("LOOPSBENCH_AGENT_RATE_LIMIT_RETRIES_PER_ROUND", raising=False)
    with pytest.raises(AgentSafetyError, match=error):
        _construct_harness(tmp_path, monkeypatch, agent_kwargs=agent_kwargs)


def test_external_command_defaults_to_no_rate_limit_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LOOPSBENCH_AGENT_RATE_LIMIT_RETRIES_PER_ROUND", raising=False)

    harness = _construct_harness(tmp_path, monkeypatch, agent_kwargs={})

    assert harness._rate_limit_retries_per_round == 0


def test_external_run_metadata_redacts_argv_before_initial_write(
    tmp_path: Path,
) -> None:
    harness = _make_harness(tmp_path, AgentName.EXTERNAL_COMMAND)
    private_command = ["runner", "--opaque", "private-marker"]
    harness._agent_kwargs = {
        "command": private_command,
        "arm_id": "loopx",
        "api_key": "private-api-key",
        "unreviewed_extension_option": "private-extension-state",
    }

    metadata_kwargs = harness._metadata_agent_kwargs()

    assert harness._agent_kwargs["command"] == private_command
    assert metadata_kwargs["command"] == {
        "command_recorded": False,
        "command_argument_count": 3,
        "command_sha256": hashlib.sha256(
            json.dumps(
                private_command,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    }
    assert set(metadata_kwargs) == {"arm_id", "command"}
    rendered_metadata = json.dumps(metadata_kwargs)
    assert "private-marker" not in rendered_metadata
    assert "private-api-key" not in rendered_metadata
    assert "private-extension-state" not in rendered_metadata


def test_external_preseed_rejection_precedes_first_metadata_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = _make_task(tmp_path)
    harness = _construct_harness(
        tmp_path,
        monkeypatch,
        agent_kwargs={
            "command": ["runner"],
            "arm_id": "loopx",
            "expected_provider": "trae",
            "provider_authority": "trae-adapter",
            "integrity_policy_sha256": "a" * 64,
            "api_key": "private-api-key",
        },
    )
    harness._dataset = SimpleNamespace(
        task_ids=[task_dir.name],
        _path=task_dir.parent,
        __len__=lambda: 1,
    )

    with pytest.raises(AgentSafetyError, match="credential_isolation_unavailable"):
        harness._write_run_metadata()

    assert not harness._run_metadata_output_path.exists()


def test_external_command_allows_predeclared_run_level_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _construct_harness(tmp_path, monkeypatch, agent_kwargs={}, n_attempts=2)

    assert harness._n_attempts == 2


def test_external_command_rejects_environment_rate_limit_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOOPSBENCH_AGENT_RATE_LIMIT_RETRIES_PER_ROUND", "1")

    with pytest.raises(AgentSafetyError, match="rate_limit_retries_per_round=0"):
        _construct_harness(tmp_path, monkeypatch, agent_kwargs={})


def test_run_agent_raises_when_agent_returns_safety_failure(tmp_path: Path) -> None:
    harness = Harness.__new__(Harness)

    with pytest.raises(AgentSafetyError, match="agent_safety_error"):
        harness._run_agent(
            session=None,
            handler=SimpleNamespace(
                task=SimpleNamespace(max_agent_timeout_sec=60),
                task_id="task_compiler",
            ),
            agent=_SafetyResultAgent(),  # type: ignore[arg-type]
            instruction="implement the task",
            logging_dir=tmp_path,
            timeout_sec=1,
        )


def test_promote_round_artifacts_promotes_external_command_result(
    tmp_path: Path,
) -> None:
    harness = Harness.__new__(Harness)
    round_logging_dir = tmp_path / "outer-loop" / "round-02"
    result_path = round_logging_dir / "trajectories" / "result.public.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text('{"status":"succeeded"}\n', encoding="utf-8")
    base_logging_dir = tmp_path / "agent-logs"

    harness._promote_round_artifacts(round_logging_dir, base_logging_dir)

    promoted = base_logging_dir / "trajectories" / "result.public.json"
    assert promoted.read_bytes() == result_path.read_bytes()


def test_external_harness_destroys_before_result_parse_or_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = _make_task(tmp_path)
    harness = _make_harness(tmp_path, AgentName.EXTERNAL_COMMAND)
    harness._agent_kwargs = {
        "command": ["runner"],
        "arm_id": "loopx",
        "expected_provider": "trae",
        "provider_authority": "trae-adapter",
        "integrity_policy_sha256": "a" * 64,
    }
    events: list[str] = []
    workspace = _workspace_archive()
    snapshot = ExternalCommandSnapshot(
        container_id="solver-id",
        post_agent_pane="captured pane",
        workspace_archive=workspace,
        raw_result_archive=b"captured result archive",
        timed_out=False,
        execution_error=None,
        snapshot_error=None,
        instruction="instruction",
        containment_receipt_ref="receipt",
        launch_binding_digest="a" * 64,
        launch_admission={},
        logging_dir=tmp_path / "agent-logs",
        command_argument_count=1,
        duration_ms=1,
    )

    agent = object.__new__(ExternalCommandAgent)
    agent._snapshot = snapshot
    agent.perform_task_with_evidence = lambda *_args, **_kwargs: (
        events.append("agent") or AgentResult()
    )
    agent.persist_containment_absence_proof = lambda *_args, **_kwargs: events.append(
        "proof"
    )
    agent.finalize_snapshot = lambda value: (
        events.append("parse")
        or AgentResult(failure_mode=FailureMode.UNKNOWN_AGENT_ERROR.value)
    )
    agent.get_trajectory_paths = lambda: []

    class _Manager:
        image_resolution = DockerImageResolution(
            requested_client_image_ref="image", resolved_image_id="sha256:image"
        )

        def destroy_external_client(self, *, expected_container_id: str) -> None:
            assert expected_container_id == "solver-id"
            events.append("destroy-not-found")

        def validate_live_client(self) -> DockerImageResolution:
            return SimpleNamespace(
                container_id="solver-id",
                started_at="2026-09-03T00:00:00Z",
                image_id="sha256:image",
            )

    class _HarnessSession:
        def capture_pane(self, *, capture_entire: bool) -> str:
            events.append("unexpected-client-capture")
            return ""

    class _HarnessTerminal(AbstractContextManager):
        def __init__(self) -> None:
            self.compose_manager = _Manager()
            self.tester_container = object()

        def create_session(self, name: str, **kwargs: object) -> _HarnessSession:
            if name.startswith("tests-"):
                events.append("tester-session")
            return _HarnessSession()

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        "loopsbench.harness.harness.spin_up_terminal",
        lambda **_kwargs: _HarnessTerminal(),
    )
    monkeypatch.setattr(
        harness, "_create_agent_for_task", lambda *_args, **_kwargs: agent
    )
    monkeypatch.setattr(harness, "_init_workspace_git", lambda *_, **__: None)
    monkeypatch.setattr(
        harness, "_start_patch_timeline_recorder", lambda *_: (None, None)
    )
    monkeypatch.setattr(harness, "_stop_patch_timeline_recorder", lambda *_: None)
    monkeypatch.setattr(harness, "_stop_regression_harness", lambda *_: None)
    monkeypatch.setattr(
        harness, "_collect_external_workspace_artifacts", lambda *_: None
    )

    def run_tests(
        _terminal: object,
        _session: object,
        handler: TrialHandler,
        *,
        captured_workspace_archive: bytes | None = None,
    ) -> tuple[FailureMode, str | None]:
        assert captured_workspace_archive == workspace
        events.append("verifier")
        handler.trial_paths.post_test_pane_path.write_text("tests skipped\n")
        return FailureMode.TEST_TIMEOUT, "tester-id"

    monkeypatch.setattr(harness, "_run_tests", run_tests)

    result = harness._execute_single_trial("task_compiler.1-of-1.test_run", task_dir)

    assert result.failure_mode == FailureMode.TEST_TIMEOUT
    assert events.index("destroy-not-found") < events.index("parse")
    assert events.index("destroy-not-found") < events.index("verifier")
    assert "unexpected-client-capture" not in events
    assert not (
        tmp_path
        / "runs"
        / "test_run"
        / "task_compiler"
        / "task_compiler.1-of-1.test_run"
        / "panes"
        / "pre-agent.txt"
    ).exists()


@pytest.mark.parametrize(
    (
        "parse_failure",
        "verifier_resolved",
        "expected_failure",
        "expected_is_resolved",
    ),
    [
        (FailureMode.PARSE_ERROR, False, FailureMode.PARSE_ERROR, None),
        (FailureMode.NONE, False, FailureMode.UNKNOWN_AGENT_ERROR, False),
        (FailureMode.NONE, True, FailureMode.NONE, True),
    ],
)
def test_external_verifier_remains_scoring_authority_after_solver_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parse_failure: FailureMode,
    verifier_resolved: bool,
    expected_failure: FailureMode,
    expected_is_resolved: bool | None,
) -> None:
    task_dir = _make_task(tmp_path)
    harness = _make_harness(tmp_path, AgentName.EXTERNAL_COMMAND)
    harness._agent_kwargs = {
        "command": ["runner"],
        "arm_id": "loopx",
        "expected_provider": "trae",
        "provider_authority": "trae-adapter",
        "integrity_policy_sha256": "a" * 64,
    }
    private_pane_marker = "root@private-container:/workspace# "
    workspace = _workspace_archive()
    snapshot = ExternalCommandSnapshot(
        container_id="solver-id",
        post_agent_pane=(
            "External solver pane output was not retained by LoopsBench.\n"
        ),
        workspace_archive=workspace,
        raw_result_archive=b"captured result archive",
        timed_out=False,
        execution_error=None,
        snapshot_error=None,
        instruction="instruction",
        containment_receipt_ref="receipt",
        launch_binding_digest="a" * 64,
        launch_admission={},
        logging_dir=tmp_path / "agent-logs",
        command_argument_count=1,
        duration_ms=1,
    )

    agent = object.__new__(ExternalCommandAgent)
    agent._snapshot = snapshot
    agent.perform_task_with_evidence = lambda *_args, **_kwargs: AgentResult()
    agent.persist_containment_absence_proof = lambda *_args, **_kwargs: None
    agent.finalize_snapshot = lambda _value: AgentResult(
        failure_mode=FailureMode.UNKNOWN_AGENT_ERROR.value
    )
    agent.get_trajectory_paths = lambda: []

    class _Manager:
        image_resolution = DockerImageResolution(
            requested_client_image_ref="image", resolved_image_id="sha256:image"
        )

        def destroy_external_client(self, *, expected_container_id: str) -> None:
            assert expected_container_id == "solver-id"

        def validate_live_client(self) -> DockerImageResolution:
            return SimpleNamespace(
                container_id="solver-id",
                started_at="2026-09-03T00:00:00Z",
                image_id="sha256:image",
            )

    class _PrivatePaneSession:
        def capture_pane(self, *, capture_entire: bool) -> str:
            pytest.fail(
                f"external client pane must remain private: {private_pane_marker}"
            )

    class _HarnessTerminal(AbstractContextManager):
        def __init__(self) -> None:
            self.compose_manager = _Manager()
            self.tester_container = object()

        def create_session(self, _name: str, **_kwargs: object) -> _PrivatePaneSession:
            return _PrivatePaneSession()

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        "loopsbench.harness.harness.spin_up_terminal",
        lambda **_kwargs: _HarnessTerminal(),
    )
    monkeypatch.setattr(
        harness, "_create_agent_for_task", lambda *_args, **_kwargs: agent
    )
    monkeypatch.setattr(harness, "_init_workspace_git", lambda *_, **__: None)
    monkeypatch.setattr(
        harness, "_start_patch_timeline_recorder", lambda *_: (None, None)
    )
    monkeypatch.setattr(harness, "_stop_patch_timeline_recorder", lambda *_: None)
    monkeypatch.setattr(harness, "_stop_regression_harness", lambda *_: None)
    monkeypatch.setattr(
        harness, "_collect_external_workspace_artifacts", lambda *_: None
    )

    def run_tests(
        _terminal: object,
        _session: object,
        handler: TrialHandler,
        **_kwargs: object,
    ) -> tuple[FailureMode, str | None]:
        handler.trial_paths.post_test_pane_path.write_text("verifier output\n")
        return FailureMode.NONE, "tester-id"

    monkeypatch.setattr(harness, "_run_tests", run_tests)
    monkeypatch.setattr(
        harness,
        "_parse_result_artifacts",
        lambda *_: ([], parse_failure),
    )
    if verifier_resolved:
        monkeypatch.setattr(
            harness,
            "_merge_result_sets",
            lambda *_: {"case": UnitTestStatus.PASSED},
        )

    result = harness._execute_single_trial("task_compiler.1-of-1.test_run", task_dir)

    assert result.failure_mode == expected_failure
    assert result.is_resolved is expected_is_resolved
    assert result.raw_output_paths is not None
    assert "pre_agent" not in result.raw_output_paths
    pre_agent_path = (
        tmp_path
        / "runs"
        / "test_run"
        / "task_compiler"
        / "task_compiler.1-of-1.test_run"
        / "panes"
        / "pre-agent.txt"
    )
    assert not pre_agent_path.exists()
    assert all(
        private_pane_marker.encode() not in path.read_bytes()
        for path in pre_agent_path.parent.parent.rglob("*")
        if path.is_file()
    )


@pytest.mark.parametrize(
    ("cleanup_exit_code", "transfer_result", "error"),
    [
        (1, True, "external_agent_tester_workspace_cleanup_failed"),
        (0, False, "external_agent_workspace_transfer_failed"),
    ],
)
def test_external_workspace_transfer_is_exact_and_fails_before_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_exit_code: int,
    transfer_result: bool,
    error: str,
) -> None:
    task_dir = _make_task(tmp_path)
    handler = TrialHandler(
        trial_name="task_compiler.1-of-1.test_run",
        input_path=task_dir,
        output_path=tmp_path / "runs",
    )
    harness = _make_harness(tmp_path, AgentName.EXTERNAL_COMMAND)
    tester = _SnapshotTester(
        cleanup_exit_code=cleanup_exit_code, transfer_result=transfer_result
    )
    terminal = _external_terminal(tester)
    verifier_calls = 0

    class _Runner:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def run(self, **_kwargs: object) -> None:
            nonlocal verifier_calls
            verifier_calls += 1

    monkeypatch.setattr("loopsbench.harness.test_runner.TestRunner", _Runner)
    archive = _workspace_archive()

    with pytest.raises(AgentSafetyError, match=error):
        harness._run_tests(
            terminal,
            object(),
            handler,
            captured_workspace_archive=archive,
        )

    assert verifier_calls == 0
    if cleanup_exit_code == 0:
        assert tester.received == archive


def test_external_workspace_transfer_uses_only_captured_archive(
    tmp_path: Path,
) -> None:
    task_dir = _make_task(tmp_path)
    handler = TrialHandler(
        trial_name="task_compiler.1-of-1.test_run",
        input_path=task_dir,
        output_path=tmp_path / "runs",
    )
    harness = _make_harness(tmp_path, AgentName.EXTERNAL_COMMAND)
    tester = _SnapshotTester()
    archive = _workspace_archive()
    terminal = _external_terminal(tester)
    terminal._container = SimpleNamespace(
        get_archive=lambda *_: pytest.fail(
            "destroyed client must not be used for tester transfer"
        ),
    )

    harness._sync_workspace_to_tester(
        terminal, handler, captured_workspace_archive=archive
    )

    assert tester.received == archive


def test_external_result_artifacts_use_immutable_tester_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = _make_task(tmp_path)
    handler = TrialHandler(
        trial_name="task_compiler.1-of-1.test_run",
        input_path=task_dir,
        output_path=tmp_path / "runs",
    )
    harness = _make_harness(tmp_path, AgentName.EXTERNAL_COMMAND)
    results = SimpleNamespace(raw_output_paths=None, result_artifacts=None)
    source = SimpleNamespace(
        format="junit_xml",
        path="results.xml",
        required=False,
        fallback=False,
    )
    handler.task = SimpleNamespace(get_result_sources=lambda: [source])
    refs: list[str] = []

    def _copy(
        _handler: TrialHandler,
        _src: str,
        _dest: Path,
        _label: str,
        *,
        container_ref: str | None = None,
    ) -> bool:
        refs.append(str(container_ref))
        return True

    monkeypatch.setattr(harness, "_copy_container_file", _copy)

    harness._collect_result_artifacts(
        handler,
        results,  # type: ignore[arg-type]
        "post test output\n",
        tester_container_ref="tester-immutable-id",
    )

    assert refs == ["tester-immutable-id"]


def test_external_result_artifacts_fail_closed_without_immutable_tester_id(
    tmp_path: Path,
) -> None:
    task_dir = _make_task(tmp_path)
    handler = TrialHandler(
        trial_name="task_compiler.1-of-1.test_run",
        input_path=task_dir,
        output_path=tmp_path / "runs",
    )
    harness = _make_harness(tmp_path, AgentName.EXTERNAL_COMMAND)
    results = SimpleNamespace(raw_output_paths=None, result_artifacts=None)
    source = SimpleNamespace(
        format="junit_xml",
        path="results.xml",
        required=False,
        fallback=False,
    )
    handler.task = SimpleNamespace(get_result_sources=lambda: [source])

    with pytest.raises(
        AgentSafetyError, match="external_agent_tester_container_identity_unavailable"
    ):
        harness._collect_result_artifacts(
            handler,
            results,  # type: ignore[arg-type]
            "post test output\n",
            tester_container_ref=None,
        )


def test_execute_single_trial_fails_closed_before_tests_on_safety_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_dir = _make_task(tmp_path)
    harness = _make_harness(tmp_path, AgentName.ORACLE)
    test_calls = 0
    terminal_kwargs: dict[str, object] = {}

    def _unexpected_tests(
        *_args: object, **_kwargs: object
    ) -> tuple[FailureMode, str | None]:
        nonlocal test_calls
        test_calls += 1
        raise AssertionError("_run_tests must not run after an agent safety error")

    def _terminal(**kwargs: object) -> _Terminal:
        terminal_kwargs.update(kwargs)
        return _Terminal()

    monkeypatch.setattr("loopsbench.harness.harness.spin_up_terminal", _terminal)
    monkeypatch.setattr(
        harness,
        "_create_agent_for_task",
        lambda *_args, **_kwargs: _SafetyResultAgent(),
    )
    monkeypatch.setattr(harness, "_init_workspace_git", lambda *_, **__: None)
    monkeypatch.setattr(
        harness, "_start_patch_timeline_recorder", lambda *_: (None, None)
    )
    monkeypatch.setattr(harness, "_collect_agent_tests", lambda *_: None)
    monkeypatch.setattr(harness, "_collect_agent_artifacts", lambda *_: None)
    monkeypatch.setattr(harness, "_persist_workspace_git", lambda *_: None)
    monkeypatch.setattr(harness, "_run_tests", _unexpected_tests)

    result = harness._execute_single_trial("task_compiler.1-of-1.test_run", task_dir)

    assert test_calls == 0
    assert result.failure_mode == FailureMode.AGENT_SAFETY_ERROR
    assert result.test_started_at is None
    assert result.test_ended_at is None
    assert result.is_resolved is None
    assert terminal_kwargs["external_command_isolation"] is False


def test_external_execution_exception_fails_closed_before_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_dir = _make_task(tmp_path)
    harness = _make_harness(tmp_path, AgentName.EXTERNAL_COMMAND)
    harness._agent_kwargs = {
        "command": ["runner"],
        "arm_id": "loopx",
        "expected_provider": "trae",
        "provider_authority": "trae-adapter",
        "integrity_policy_sha256": "a" * 64,
    }
    verifier_calls = 0

    def _unexpected_tests(
        *_args: object, **_kwargs: object
    ) -> tuple[FailureMode, str | None]:
        nonlocal verifier_calls
        verifier_calls += 1
        raise AssertionError("verifier must not run after execution safety failure")

    monkeypatch.setattr(
        "loopsbench.harness.harness.spin_up_terminal", lambda **_: _Terminal()
    )
    monkeypatch.setattr(
        harness,
        "_create_agent_for_task",
        lambda *_args, **_kwargs: _RaisingSafetyAgent(),
    )
    monkeypatch.setattr(harness, "_init_workspace_git", lambda *_, **__: None)
    monkeypatch.setattr(
        harness, "_start_patch_timeline_recorder", lambda *_: (None, None)
    )
    monkeypatch.setattr(harness, "_stop_patch_timeline_recorder", lambda *_: None)
    monkeypatch.setattr(harness, "_stop_regression_harness", lambda *_: None)
    monkeypatch.setattr(harness, "_collect_agent_tests", lambda *_: None)
    monkeypatch.setattr(harness, "_collect_agent_artifacts", lambda *_: None)
    monkeypatch.setattr(harness, "_persist_workspace_git", lambda *_: None)
    monkeypatch.setattr(harness, "_run_tests", _unexpected_tests)

    result = harness._execute_single_trial("task_compiler.1-of-1.test_run", task_dir)

    assert verifier_calls == 0
    assert result.failure_mode == FailureMode.AGENT_SAFETY_ERROR
    assert result.test_started_at is None
    assert result.test_ended_at is None
    assert result.is_resolved is None


def test_external_command_enables_compose_isolation_before_agent_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_dir = _make_task(tmp_path)
    harness = _make_harness(tmp_path, AgentName.EXTERNAL_COMMAND)
    harness._agent_kwargs = {
        "command": ["runner"],
        "arm_id": "loopx",
        "expected_provider": "trae",
        "expected_model": "GPT-5.4",
        "provider_authority": "trae-adapter",
        "integrity_policy_sha256": "a" * 64,
    }
    terminal_kwargs: dict[str, object] = {}

    def _terminal(**kwargs: object) -> _Terminal:
        terminal_kwargs.update(kwargs)
        return _Terminal()

    monkeypatch.setattr("loopsbench.harness.harness.spin_up_terminal", _terminal)
    monkeypatch.setattr(
        harness,
        "_create_agent_for_task",
        lambda *_args, **_kwargs: _SafetyResultAgent(),
    )
    monkeypatch.setattr(harness, "_init_workspace_git", lambda *_, **__: None)
    monkeypatch.setattr(
        harness, "_start_patch_timeline_recorder", lambda *_: (None, None)
    )
    monkeypatch.setattr(harness, "_collect_agent_tests", lambda *_: None)
    monkeypatch.setattr(harness, "_collect_agent_artifacts", lambda *_: None)
    monkeypatch.setattr(harness, "_persist_workspace_git", lambda *_: None)

    result = harness._execute_single_trial("task_compiler.1-of-1.test_run", task_dir)

    assert result.failure_mode == FailureMode.AGENT_SAFETY_ERROR
    assert terminal_kwargs["external_command_isolation"] is True


def test_external_command_invalid_preseed_stops_before_terminal_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_dir = _make_task(tmp_path)
    harness = _make_harness(tmp_path, AgentName.EXTERNAL_COMMAND)
    harness._agent_kwargs = {
        "command": ["runner"],
        "arm_id": "loopx",
        # expected_provider is deliberately absent.
        "expected_model": "GPT-5.4",
        "provider_authority": "trae-adapter",
        "integrity_policy_sha256": "a" * 64,
    }
    terminal_start_calls = 0

    def _unexpected_terminal(**_kwargs: object) -> _Terminal:
        nonlocal terminal_start_calls
        terminal_start_calls += 1
        raise AssertionError("terminal must not start before preseed admission")

    monkeypatch.setattr(
        "loopsbench.harness.harness.spin_up_terminal", _unexpected_terminal
    )

    result = harness._execute_single_trial("task_compiler.1-of-1.test_run", task_dir)

    assert terminal_start_calls == 0
    assert result.failure_mode == FailureMode.AGENT_SAFETY_ERROR
    assert result.test_started_at is None
    assert result.test_ended_at is None
    assert result.is_resolved is None


def test_external_command_preprovision_rejection_is_agent_safety_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_dir = _make_task(tmp_path)
    harness = _make_harness(tmp_path, AgentName.EXTERNAL_COMMAND)
    harness._agent_kwargs = {
        "command": ["runner"],
        "arm_id": "loopx",
        "expected_provider": "trae",
        "expected_model": "untrusted-kwarg-model",
        "provider_authority": "trae-adapter",
        "integrity_policy_sha256": "a" * 64,
    }
    verifier_calls = 0
    stop_calls = 0

    class _RejectingComposeManager:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            raise ContainerAdmissionError("credential_isolation_unavailable")

        def stop(self) -> None:
            nonlocal stop_calls
            stop_calls += 1

    def _unexpected_tests(
        *_args: object, **_kwargs: object
    ) -> tuple[FailureMode, str | None]:
        nonlocal verifier_calls
        verifier_calls += 1
        raise AssertionError("verifier must not run after pre-provision rejection")

    monkeypatch.setattr(
        "loopsbench.terminal.terminal.DockerComposeManager",
        _RejectingComposeManager,
    )
    monkeypatch.setattr(harness, "_run_tests", _unexpected_tests)

    result = harness._execute_single_trial("task_compiler.1-of-1.test_run", task_dir)

    assert verifier_calls == 0
    assert stop_calls == 1
    assert result.failure_mode == FailureMode.AGENT_SAFETY_ERROR
    assert result.test_started_at is None
    assert result.test_ended_at is None
    assert result.is_resolved is None


@pytest.mark.parametrize(
    "agent_name",
    [AgentName.ORACLE, AgentName.EXTERNAL_COMMAND],
)
def test_regression_harness_does_not_start_for_safety_gated_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_name: AgentName,
) -> None:
    task_dir = _make_task(tmp_path)
    (task_dir / "tests").mkdir()
    (task_dir / "run-tests.sh").write_text("#!/bin/sh\n")
    handler = TrialHandler(
        trial_name="task_compiler.1-of-1.test_run",
        input_path=task_dir,
        output_path=tmp_path / "runs",
    )
    harness = _make_harness(tmp_path, agent_name)
    construction_calls = 0

    def _unexpected_regression_harness(_config: object) -> None:
        nonlocal construction_calls
        construction_calls += 1
        raise AssertionError("regression harness must not be constructed")

    monkeypatch.setattr(
        "loopsbench.harness.harness.RegressionHarness",
        _unexpected_regression_harness,
    )

    thread, regression_harness = harness._maybe_start_regression_harness(
        handler,
        SimpleNamespace(tester_container=object()),  # type: ignore[arg-type]
    )

    assert construction_calls == 0
    assert thread is None
    assert regression_harness is None
