from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from long_horizon_bench.cli.runs import deprecated_no_rebuild_warning
from long_horizon_bench.file_config.resolve import resolve_run_params
from long_horizon_bench.file_config.run_file import LhbRunFileConfig
from long_horizon_bench.task_images.strategy import (
    DockerImageStrategy,
    local_client_image_name,
    normalize_task_id,
    remote_client_image_ref,
    resolve_docker_image_strategy,
    task_image_repo,
    validate_remote_docker_image_coordinates,
)


def _resolve(**overrides):
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
    return resolve_run_params(None, **base)


def test_strategy_helpers_normalize_and_build_image_refs() -> None:
    task_id = "task_hadoop_seg05"
    assert normalize_task_id(task_id) == "task-hadoop-seg05"
    assert local_client_image_name(task_id) == "lhb__task_hadoop_seg05__client"
    assert task_image_repo("exampleorg", task_id) == (
        "exampleorg/lhb-task-hadoop-seg05"
    )
    assert remote_client_image_ref("exampleorg", task_id, "git-ab12cd3") == (
        "exampleorg/lhb-task-hadoop-seg05:git-ab12cd3"
    )


def test_resolve_run_params_defaults_to_remote_strategy() -> None:
    rp = _resolve()
    assert rp["docker_image_strategy"] == DockerImageStrategy.REMOTE
    assert rp["docker_image_namespace"] is None
    assert rp["docker_image_tag"] is None


def test_no_rebuild_maps_to_local_existing_strategy() -> None:
    rp = _resolve(no_rebuild=True)
    assert rp["no_rebuild"] is True
    assert rp["docker_image_strategy"] == DockerImageStrategy.LOCAL_EXISTING


def test_run_file_accepts_docker_image_fields() -> None:
    cfg = LhbRunFileConfig.model_validate(
        {
            "docker_image_strategy": "local-build",
            "docker_image_namespace": "exampleorg",
            "docker_image_tag": "git-ab12cd3",
        }
    )
    assert cfg.docker_image_strategy == DockerImageStrategy.LOCAL_BUILD
    assert cfg.docker_image_namespace == "exampleorg"
    assert cfg.docker_image_tag == "git-ab12cd3"


def test_resolve_run_params_rejects_mixed_deprecated_and_explicit_strategy() -> None:
    with pytest.raises(ValueError, match="Cannot combine deprecated no_rebuild"):
        _resolve(
            no_rebuild=True,
            docker_image_strategy=DockerImageStrategy.REMOTE,
        )


def test_resolve_docker_image_strategy_defaults_to_remote() -> None:
    assert (
        resolve_docker_image_strategy(
            no_rebuild=None,
            explicit_strategy=None,
        )
        == DockerImageStrategy.REMOTE
    )


def test_validate_remote_strategy_requires_namespace_and_tag() -> None:
    with pytest.raises(
        ValueError,
        match=(
            "docker_image_namespace and docker_image_tag are required when "
            "docker_image_strategy=remote"
        ),
    ):
        validate_remote_docker_image_coordinates(
            strategy=DockerImageStrategy.REMOTE,
            docker_image_namespace=None,
            docker_image_tag=None,
        )

    validate_remote_docker_image_coordinates(
        strategy=DockerImageStrategy.LOCAL_BUILD,
        docker_image_namespace=None,
        docker_image_tag=None,
    )
    validate_remote_docker_image_coordinates(
        strategy=DockerImageStrategy.REMOTE,
        docker_image_namespace="exampleorg",
        docker_image_tag="git-ab12cd3",
    )


def test_deprecated_no_rebuild_warning_mentions_replacement() -> None:
    warning = deprecated_no_rebuild_warning(True)
    assert warning is not None
    assert "deprecated" in warning.lower()
    assert "local-existing" in warning
