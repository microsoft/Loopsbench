from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from loopsbench.cli.runs import deprecated_no_rebuild_warning
from loopsbench.file_config.resolve import resolve_run_params
from loopsbench.file_config.run_file import LoopsBenchRunFileConfig
from loopsbench.task_images.strategy import (
    DEFAULT_REMOTE_DOCKER_IMAGE_TAG,
    DockerImageStrategy,
    local_client_image_name,
    normalize_task_id,
    remote_client_image_ref,
    resolve_docker_image_strategy,
    resolve_task_docker_image,
    task_image_repo,
    validate_remote_docker_image_coordinates,
)


def _resolve(file_cfg=None, **overrides):
    base = dict(
        dataset_path=None,
        output_path=None,
        run_id=None,
        task_ids=None,
        n_tasks=None,
        exclude_task_ids=None,
        no_rebuild=None,
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
        docker_image_strategy=None,
        docker_image_namespace=None,
        docker_image_tag=None,
    )
    base.update(overrides)
    return resolve_run_params(file_cfg, **base)


def test_strategy_helpers_normalize_and_build_image_refs() -> None:
    task_id = "task_hadoop_seg05"
    assert normalize_task_id(task_id) == "task-hadoop-seg05"
    assert local_client_image_name(task_id) == "loopsbench__task_hadoop_seg05__client"
    assert task_image_repo("exampleorg", task_id) == (
        "exampleorg/loopsbench-task-hadoop-seg05"
    )
    assert remote_client_image_ref("exampleorg", task_id, "git-ab12cd3") == (
        "exampleorg/loopsbench-task-hadoop-seg05:git-ab12cd3"
    )


@pytest.mark.parametrize("file_cfg", [None, LoopsBenchRunFileConfig()])
def test_resolve_run_params_defaults_to_local_build(file_cfg) -> None:
    rp = _resolve(file_cfg)
    assert rp["docker_image_strategy"] == DockerImageStrategy.LOCAL_BUILD
    assert rp["docker_image_namespace"] is None
    assert rp["docker_image_tag"] is None
    assert rp["docker_image_tag_defaulted"] is False
    image = resolve_task_docker_image(
        task_id="task_hadoop_seg05",
        strategy=rp["docker_image_strategy"],
        docker_image_namespace=rp["docker_image_namespace"],
        docker_image_tag=rp["docker_image_tag"],
    )
    assert image.client_image_ref == "loopsbench__task_hadoop_seg05__client"
    assert image.build_required is True
    assert image.pull_required is False


@pytest.mark.parametrize(
    ("file_cfg", "overrides"),
    [
        (None, {"docker_image_strategy": DockerImageStrategy.REMOTE}),
        (LoopsBenchRunFileConfig(docker_image_strategy="remote"), {}),
    ],
)
def test_explicit_remote_defaults_to_loopsbench_latest(file_cfg, overrides) -> None:
    rp = _resolve(file_cfg, **overrides)
    assert rp["docker_image_strategy"] == DockerImageStrategy.REMOTE
    assert rp["docker_image_namespace"] == "loopsbench"
    assert rp["docker_image_tag"] == DEFAULT_REMOTE_DOCKER_IMAGE_TAG
    assert rp["docker_image_tag_defaulted"] is True


def test_resolve_run_params_preserves_explicit_remote_tag() -> None:
    rp = _resolve(
        docker_image_strategy=DockerImageStrategy.REMOTE,
        docker_image_namespace="exampleorg",
        docker_image_tag="git-ab12cd3",
    )
    assert rp["docker_image_strategy"] == DockerImageStrategy.REMOTE
    assert rp["docker_image_namespace"] == "exampleorg"
    assert rp["docker_image_tag"] == "git-ab12cd3"
    assert rp["docker_image_tag_defaulted"] is False


def test_resolve_run_params_empty_remote_tag_defaults_to_latest() -> None:
    rp = _resolve(
        docker_image_strategy=DockerImageStrategy.REMOTE,
        docker_image_namespace="exampleorg",
        docker_image_tag="",
    )
    assert rp["docker_image_strategy"] == DockerImageStrategy.REMOTE
    assert rp["docker_image_namespace"] == "exampleorg"
    assert rp["docker_image_tag"] == DEFAULT_REMOTE_DOCKER_IMAGE_TAG
    assert rp["docker_image_tag_defaulted"] is True


def test_resolve_run_params_local_build_does_not_synthesize_tag() -> None:
    rp = _resolve(docker_image_strategy=DockerImageStrategy.LOCAL_BUILD)
    assert rp["docker_image_strategy"] == DockerImageStrategy.LOCAL_BUILD
    assert rp["docker_image_tag"] is None
    assert rp["docker_image_tag_defaulted"] is False


def test_no_rebuild_maps_to_local_existing_strategy() -> None:
    rp = _resolve(no_rebuild=True)
    assert rp["no_rebuild"] is True
    assert rp["docker_image_strategy"] == DockerImageStrategy.LOCAL_EXISTING
    assert rp["docker_image_tag"] is None
    assert rp["docker_image_tag_defaulted"] is False


def test_run_file_accepts_docker_image_fields() -> None:
    cfg = LoopsBenchRunFileConfig.model_validate(
        {
            "docker_image_strategy": "local-build",
            "docker_image_namespace": "exampleorg",
            "docker_image_tag": "git-ab12cd3",
        }
    )
    assert cfg.docker_image_strategy == DockerImageStrategy.LOCAL_BUILD
    assert cfg.docker_image_namespace == "exampleorg"
    assert cfg.docker_image_tag == "git-ab12cd3"


def test_remote_run_file_coordinates_and_cli_overrides() -> None:
    cfg = LoopsBenchRunFileConfig(
        docker_image_strategy="remote",
        docker_image_namespace="customorg",
        docker_image_tag="release",
    )
    rp = _resolve(cfg)
    assert rp["docker_image_strategy"] == DockerImageStrategy.REMOTE
    assert rp["docker_image_namespace"] == "customorg"
    assert rp["docker_image_tag"] == "release"
    rp = _resolve(cfg, docker_image_namespace="loopsbench", docker_image_tag="latest")
    assert rp["docker_image_namespace"] == "loopsbench"
    assert rp["docker_image_tag"] == "latest"
    assert rp["docker_image_tag_defaulted"] is False
    rp = _resolve(cfg, docker_image_strategy=DockerImageStrategy.LOCAL_BUILD)
    assert rp["docker_image_strategy"] == DockerImageStrategy.LOCAL_BUILD


def test_resolve_run_params_rejects_mixed_deprecated_and_explicit_strategy() -> None:
    with pytest.raises(ValueError, match="Cannot combine deprecated no_rebuild"):
        _resolve(
            no_rebuild=True,
            docker_image_strategy=DockerImageStrategy.REMOTE,
        )


def test_resolve_docker_image_strategy_defaults_to_local_build() -> None:
    assert (
        resolve_docker_image_strategy(
            no_rebuild=None,
            explicit_strategy=None,
        )
        == DockerImageStrategy.LOCAL_BUILD
    )


def test_validate_remote_coordinates_require_resolved_namespace_but_not_tag() -> None:
    with pytest.raises(
        ValueError,
        match="docker_image_namespace is required when docker_image_strategy=remote",
    ):
        validate_remote_docker_image_coordinates(
            strategy=DockerImageStrategy.REMOTE,
            docker_image_namespace=None,
            docker_image_tag=None,
        )
    with pytest.raises(
        ValueError,
        match="docker_image_namespace is required when docker_image_strategy=remote",
    ):
        validate_remote_docker_image_coordinates(
            strategy=DockerImageStrategy.REMOTE,
            docker_image_namespace=None,
            docker_image_tag="git-ab12cd3",
        )

    validate_remote_docker_image_coordinates(
        strategy=DockerImageStrategy.LOCAL_BUILD,
        docker_image_namespace=None,
        docker_image_tag=None,
    )
    validate_remote_docker_image_coordinates(
        strategy=DockerImageStrategy.REMOTE,
        docker_image_namespace="exampleorg",
        docker_image_tag=None,
    )
    validate_remote_docker_image_coordinates(
        strategy=DockerImageStrategy.REMOTE,
        docker_image_namespace="exampleorg",
        docker_image_tag="git-ab12cd3",
    )


@pytest.mark.parametrize("namespace", [None, ""])
def test_resolve_task_docker_image_defaults_to_loopsbench(namespace) -> None:
    resolved = resolve_task_docker_image(
        task_id="task_NodeBB_seg05",
        strategy=DockerImageStrategy.REMOTE,
        docker_image_namespace=namespace,
        docker_image_tag=None,
    )
    assert resolved.client_image_ref == "loopsbench/loopsbench-task-nodebb-seg05:latest"
    assert resolved.pull_required is True
    assert resolved.build_required is False


def test_resolve_task_docker_image_defaults_remote_tag_to_latest() -> None:
    resolved = resolve_task_docker_image(
        task_id="task_hadoop_seg05",
        strategy=DockerImageStrategy.REMOTE,
        docker_image_namespace="exampleorg",
        docker_image_tag=None,
    )
    assert resolved.client_image_ref == remote_client_image_ref(
        "exampleorg",
        "task_hadoop_seg05",
        DEFAULT_REMOTE_DOCKER_IMAGE_TAG,
    )
    assert resolved.pull_required is True
    assert resolved.build_required is False


def test_resolve_task_docker_image_empty_remote_tag_defaults_to_latest() -> None:
    resolved = resolve_task_docker_image(
        task_id="task_hadoop_seg05",
        strategy=DockerImageStrategy.REMOTE,
        docker_image_namespace="exampleorg",
        docker_image_tag="",
    )
    assert resolved.client_image_ref == remote_client_image_ref(
        "exampleorg",
        "task_hadoop_seg05",
        DEFAULT_REMOTE_DOCKER_IMAGE_TAG,
    )
    assert resolved.pull_required is True
    assert resolved.build_required is False


def test_deprecated_no_rebuild_warning_mentions_replacement() -> None:
    warning = deprecated_no_rebuild_warning(True)
    assert warning is not None
    assert "deprecated" in warning.lower()
    assert "local-existing" in warning
