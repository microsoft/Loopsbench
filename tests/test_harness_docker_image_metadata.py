from __future__ import annotations

import logging
import sys
from contextlib import AbstractContextManager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest

from loopsbench.agents.agent_name import AgentName
from loopsbench.file_config.resolve import resolve_run_params
from loopsbench.handlers.trial_handler import TrialHandler
from loopsbench.harness.harness import Harness
from loopsbench.harness.models import (
    FailureMode,
    TaskImageResolutionRecord,
    build_run_metadata,
    write_task_image_resolution,
)
from loopsbench.task_images.strategy import DockerImageStrategy
from loopsbench.terminal.docker_compose_manager import DockerImageResolution
from loopsbench.terminal.terminal import TerminalStartupError


class _StubDataset:
    def sort_by_duration(self) -> None:
        return None

    def __len__(self) -> int:
        return 0


def test_build_run_metadata_includes_docker_image_fields(tmp_path: Path) -> None:
    metadata = build_run_metadata(
        run_id="2026-07-02__12-00-00",
        dataset_path=None,
        dataset_name=None,
        dataset_version=None,
        output_path=tmp_path,
        agent_name="oracle",
        no_rebuild=False,
        cleanup=True,
        log_level=20,
        task_ids=["task_compiler"],
        exclude_task_ids=None,
        n_tasks=1,
        n_concurrent_trials=1,
        n_attempts=1,
        dataset_size=1,
        model_name="Oracle",
        commit_hash="unknown",
        username="tester",
        s3_bucket=None,
        docker_image_strategy=DockerImageStrategy.REMOTE,
        docker_image_namespace="exampleorg",
        docker_image_tag="git-ab12cd3",
        docker_image_tag_defaulted=True,
    )

    assert metadata.docker_image_strategy == "remote"
    assert metadata.docker_image_namespace == "exampleorg"
    assert metadata.docker_image_tag == "git-ab12cd3"
    assert metadata.docker_image_tag_defaulted is True


def test_write_task_image_resolution_records_requested_and_resolved_fields(
    tmp_path: Path,
) -> None:
    out = tmp_path / "task_image_resolution.json"

    record = write_task_image_resolution(
        out,
        task_id="task_hadoop_seg05",
        client_image_ref="exampleorg/loopsbench-task-hadoop-seg05:git-ab12cd3",
        image_source="remote",
        image_pull_performed=True,
        image_build_performed=False,
        resolved_image_id="sha256:client123",
        resolved_repo_digest="exampleorg/loopsbench-task-hadoop-seg05@sha256:digest456",
    )
    written = TaskImageResolutionRecord.model_validate_json(out.read_text())

    assert record.client_image_ref == "exampleorg/loopsbench-task-hadoop-seg05:git-ab12cd3"
    assert record.requested_client_image_ref == record.client_image_ref
    assert record.resolved_image_id == "sha256:client123"
    assert (
        record.resolved_repo_digest
        == "exampleorg/loopsbench-task-hadoop-seg05@sha256:digest456"
    )
    assert written == record


@pytest.mark.parametrize(
    ("kwargs", "strategy", "namespace", "tag"),
    [
        ({}, DockerImageStrategy.LOCAL_BUILD, None, None),
        ({"docker_image_strategy": "remote"}, DockerImageStrategy.REMOTE, "loopsbench", "latest"),
    ],
)
def test_direct_harness_image_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict,
    strategy: DockerImageStrategy,
    namespace: str | None,
    tag: str | None,
) -> None:
    monkeypatch.setattr(
        Harness,
        "_init_dataset",
        lambda self: setattr(self, "_dataset", _StubDataset()),
    )
    monkeypatch.setattr(Harness, "_init_agent_class", lambda self: None)
    monkeypatch.setattr(Harness, "_init_logger", lambda self: None)

    harness = Harness(
        output_path=tmp_path / "runs",
        run_id="image-defaults",
        agent_name=AgentName.ORACLE,
        **kwargs,
    )
    assert harness._docker_image_strategy == strategy
    assert harness._docker_image_namespace == namespace
    assert harness._docker_image_tag == tag
    assert harness._docker_image_tag_defaulted is (strategy == DockerImageStrategy.REMOTE)


def test_direct_harness_remote_empty_tag_forces_defaulted_true(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        Harness,
        "_init_dataset",
        lambda self: setattr(self, "_dataset", _StubDataset()),
    )
    monkeypatch.setattr(Harness, "_init_agent_class", lambda self: None)
    monkeypatch.setattr(Harness, "_init_logger", lambda self: None)

    harness = Harness(
        output_path=tmp_path / "runs",
        run_id="2026-07-03__12-00-00",
        agent_name=AgentName.ORACLE,
        docker_image_strategy=DockerImageStrategy.REMOTE,
        docker_image_namespace="exampleorg",
        docker_image_tag="",
        docker_image_tag_defaulted=False,
    )

    assert harness._docker_image_tag == "latest"
    assert harness._docker_image_tag_defaulted is True


def test_direct_harness_remote_latest_tag_preserves_caller_defaulted_true(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        Harness,
        "_init_dataset",
        lambda self: setattr(self, "_dataset", _StubDataset()),
    )
    monkeypatch.setattr(Harness, "_init_agent_class", lambda self: None)
    monkeypatch.setattr(Harness, "_init_logger", lambda self: None)

    harness = Harness(
        output_path=tmp_path / "runs",
        run_id="2026-07-03__12-30-00",
        agent_name=AgentName.ORACLE,
        docker_image_strategy=DockerImageStrategy.REMOTE,
        docker_image_namespace="exampleorg",
        docker_image_tag="latest",
        docker_image_tag_defaulted=True,
    )

    assert harness._docker_image_tag == "latest"
    assert harness._docker_image_tag_defaulted is True


def test_direct_harness_remote_latest_tag_stays_explicit_when_flag_is_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        Harness,
        "_init_dataset",
        lambda self: setattr(self, "_dataset", _StubDataset()),
    )
    monkeypatch.setattr(Harness, "_init_agent_class", lambda self: None)
    monkeypatch.setattr(Harness, "_init_logger", lambda self: None)

    harness = Harness(
        output_path=tmp_path / "runs",
        run_id="2026-07-03__12-45-00",
        agent_name=AgentName.ORACLE,
        docker_image_strategy=DockerImageStrategy.REMOTE,
        docker_image_namespace="exampleorg",
        docker_image_tag="latest",
        docker_image_tag_defaulted=False,
    )

    assert harness._docker_image_tag == "latest"
    assert harness._docker_image_tag_defaulted is False


def test_harness_preserves_defaulted_remote_tag_from_resolved_run_params(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        Harness,
        "_init_dataset",
        lambda self: setattr(self, "_dataset", _StubDataset()),
    )
    monkeypatch.setattr(Harness, "_init_agent_class", lambda self: None)
    monkeypatch.setattr(Harness, "_init_logger", lambda self: None)

    params = resolve_run_params(
        None,
        dataset_path=None,
        output_path=None,
        run_id=None,
        task_ids=None,
        n_tasks=None,
        exclude_task_ids=None,
        no_rebuild=None,
        docker_image_strategy=DockerImageStrategy.REMOTE,
        docker_image_namespace="exampleorg",
        docker_image_tag=None,
        cleanup=None,
        model_name=None,
        agent=None,
        agent_import_path=None,
        log_level_str=None,
        livestream=None,
        n_concurrent_trials=None,
        n_attempts=None,
        agent_kwargs_cli={},
        global_timeout_multiplier=None,
        global_agent_timeout_sec=None,
        global_test_timeout_sec=None,
    )

    harness = Harness(
        output_path=tmp_path / "runs",
        run_id="2026-07-03__13-00-00",
        agent_name=AgentName.ORACLE,
        docker_image_strategy=params["docker_image_strategy"],
        docker_image_namespace=params["docker_image_namespace"],
        docker_image_tag=params["docker_image_tag"],
        docker_image_tag_defaulted=params["docker_image_tag_defaulted"],
    )

    assert params["docker_image_tag"] == "latest"
    assert params["docker_image_tag_defaulted"] is True
    assert harness._docker_image_tag == "latest"
    assert harness._docker_image_tag_defaulted is True


def test_run_trial_keeps_prestart_task_image_resolution_on_generic_startup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_dir = tmp_path / "tasks" / "task_compiler"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text("instruction: test instruction\n")
    (task_dir / "docker-compose.yaml").write_text("services: {}\n")

    handler = TrialHandler(
        trial_name="task_compiler.1-of-1.test_run",
        input_path=task_dir,
        output_path=tmp_path / "runs",
    )

    harness = Harness.__new__(Harness)
    from loopsbench.terminal.network_policy import NetworkPlan
    harness._network = NetworkPlan()
    harness._dependency_service = None
    harness._task_dependency_overrides = {}
    harness._logger = logging.getLogger("test")
    harness._agent_name = AgentName.ORACLE
    harness._global_agent_timeout_sec = None
    harness._global_timeout_multiplier = 1.0
    harness._docker_image_strategy = DockerImageStrategy.REMOTE
    harness._docker_image_namespace = "exampleorg"
    harness._docker_image_tag = "git-ab12cd3"
    harness._no_rebuild = False
    harness._cleanup = True

    class _ExplodingTerminal(AbstractContextManager[None]):
        def __enter__(self) -> None:
            raise RuntimeError("boom")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    monkeypatch.setattr(
        "loopsbench.harness.harness.spin_up_terminal",
        lambda **_: _ExplodingTerminal(),
    )

    with pytest.raises(RuntimeError, match="boom"):
        harness._run_trial(handler)

    record = TaskImageResolutionRecord.model_validate_json(
        handler.trial_paths.task_image_resolution_path.read_text()
    )
    assert record.client_image_ref == "exampleorg/loopsbench-task-compiler:git-ab12cd3"
    assert record.requested_client_image_ref == record.client_image_ref
    assert record.image_source == "remote"
    assert record.resolved_image_id is None
    assert record.resolved_repo_digest is None


def test_run_trial_rewrites_task_image_resolution_from_terminal_startup_error_without_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_dir = tmp_path / "tasks" / "task_compiler"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text("instruction: test instruction\n")
    (task_dir / "docker-compose.yaml").write_text("services: {}\n")

    handler = TrialHandler(
        trial_name="task_compiler.1-of-1.test_run",
        input_path=task_dir,
        output_path=tmp_path / "runs",
    )

    harness = Harness.__new__(Harness)
    from loopsbench.terminal.network_policy import NetworkPlan
    harness._network = NetworkPlan()
    harness._dependency_service = None
    harness._task_dependency_overrides = {}
    harness._logger = logging.getLogger("test")
    harness._agent_name = AgentName.ORACLE
    harness._global_agent_timeout_sec = None
    harness._global_timeout_multiplier = 1.0
    harness._docker_image_strategy = DockerImageStrategy.REMOTE
    harness._docker_image_namespace = "exampleorg"
    harness._docker_image_tag = "git-ab12cd3"
    harness._no_rebuild = False
    harness._cleanup = True

    class _ExplodingTerminal(AbstractContextManager[None]):
        def __enter__(self) -> None:
            raise TerminalStartupError("startup failed", image_resolution=None)

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    monkeypatch.setattr(
        "loopsbench.harness.harness.spin_up_terminal",
        lambda **_: _ExplodingTerminal(),
    )

    with pytest.raises(TerminalStartupError, match="startup failed"):
        harness._run_trial(handler)

    record = TaskImageResolutionRecord.model_validate_json(
        handler.trial_paths.task_image_resolution_path.read_text()
    )
    assert record.client_image_ref == "exampleorg/loopsbench-task-compiler:git-ab12cd3"
    assert (
        record.requested_client_image_ref
        == "exampleorg/loopsbench-task-compiler:git-ab12cd3"
    )
    assert record.resolved_image_id is None
    assert record.resolved_repo_digest is None
    assert record.image_pull_performed is False
    assert record.image_build_performed is False


def test_run_trial_rewrites_task_image_resolution_from_terminal_startup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_dir = tmp_path / "tasks" / "task_compiler"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text("instruction: test instruction\n")
    (task_dir / "docker-compose.yaml").write_text("services: {}\n")

    handler = TrialHandler(
        trial_name="task_compiler.1-of-1.test_run",
        input_path=task_dir,
        output_path=tmp_path / "runs",
    )

    harness = Harness.__new__(Harness)
    from loopsbench.terminal.network_policy import NetworkPlan
    harness._network = NetworkPlan()
    harness._dependency_service = None
    harness._task_dependency_overrides = {}
    harness._logger = logging.getLogger("test")
    harness._agent_name = AgentName.ORACLE
    harness._global_agent_timeout_sec = None
    harness._global_timeout_multiplier = 1.0
    harness._docker_image_strategy = DockerImageStrategy.REMOTE
    harness._docker_image_namespace = "exampleorg"
    harness._docker_image_tag = "git-ab12cd3"
    harness._no_rebuild = False
    harness._cleanup = True

    image_resolution = DockerImageResolution(
        requested_client_image_ref="exampleorg/loopsbench-task-compiler:git-ab12cd3",
        resolved_image_id="sha256:client123",
        resolved_repo_digest="exampleorg/loopsbench-task-compiler@sha256:digest456",
    )

    class _ExplodingTerminal(AbstractContextManager[None]):
        def __enter__(self) -> None:
            raise TerminalStartupError(
                "startup failed",
                image_resolution=image_resolution,
            )

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    monkeypatch.setattr(
        "loopsbench.harness.harness.spin_up_terminal",
        lambda **_: _ExplodingTerminal(),
    )

    with pytest.raises(TerminalStartupError, match="startup failed"):
        harness._run_trial(handler)

    record = TaskImageResolutionRecord.model_validate_json(
        handler.trial_paths.task_image_resolution_path.read_text()
    )
    assert record.client_image_ref == "exampleorg/loopsbench-task-compiler:git-ab12cd3"
    assert (
        record.requested_client_image_ref
        == "exampleorg/loopsbench-task-compiler:git-ab12cd3"
    )
    assert record.resolved_image_id == "sha256:client123"
    assert (
        record.resolved_repo_digest
        == "exampleorg/loopsbench-task-compiler@sha256:digest456"
    )


def test_execute_single_trial_maps_terminal_startup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_dir = tmp_path / "tasks" / "task_compiler"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text("instruction: test instruction\n")
    (task_dir / "docker-compose.yaml").write_text("services: {}\n")

    harness = Harness.__new__(Harness)
    from loopsbench.terminal.network_policy import NetworkPlan
    harness._network = NetworkPlan()
    harness._dependency_service = None
    harness._task_dependency_overrides = {}
    harness._logger = logging.getLogger("test")
    harness._output_path = tmp_path
    harness._run_id = "runs"

    def _boom(_handler: TrialHandler) -> None:
        raise TerminalStartupError("startup failed")

    monkeypatch.setattr(harness, "_run_trial", _boom)

    result = harness._execute_single_trial(
        "task_compiler.1-of-1.test_run",
        task_dir,
    )

    assert result.failure_mode == FailureMode.AGENT_STARTUP_ERROR
