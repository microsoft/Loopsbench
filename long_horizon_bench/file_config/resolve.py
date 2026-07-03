"""Merge YAML run config with CLI options for ``lhb run``."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from long_horizon_bench.agents.agent_name import AgentName
from long_horizon_bench.file_config.io import merge_agent_kwargs
from long_horizon_bench.file_config.run_file import LhbRunFileConfig
from long_horizon_bench.task_images.strategy import (
    DockerImageStrategy,
    resolve_docker_image_strategy,
)


def _pick[T](
    cli: T | None,
    file_val: T | None,
    default: T,
) -> T:
    if cli is not None:
        return cli
    if file_val is not None:
        return file_val
    return default


def resolve_run_params(
    file_cfg: LhbRunFileConfig | None,
    *,
    dataset_path: Path | None,
    output_path: Path | None,
    run_id: str | None,
    task_ids: list[str] | None,
    n_tasks: int | None,
    exclude_task_ids: list[str] | None,
    no_rebuild: bool | None,
    docker_image_strategy: DockerImageStrategy | None,
    docker_image_namespace: str | None,
    docker_image_tag: str | None,
    cleanup: bool | None,
    model_name: str | None,
    agent: AgentName | None,
    agent_import_path: str | None,
    log_level_str: str | None,
    livestream: bool | None,
    n_concurrent_trials: int | None,
    n_attempts: int | None,
    agent_kwargs_cli: dict[str, Any],
    global_timeout_multiplier: float | None,
    global_agent_timeout_sec: float | None,
    global_test_timeout_sec: float | None,
) -> dict[str, Any]:
    """CLI ``None`` means 'not passed'; file overrides defaults; CLI overrides file."""
    f = file_cfg
    file_kw = f.agent_kwargs if f else {}

    eff_model = model_name
    if eff_model is None and f:
        eff_model = f.effective_model_name()

    eff_n_conc = n_concurrent_trials
    if eff_n_conc is None and f:
        eff_n_conc = f.effective_n_concurrent()
    if eff_n_conc is None:
        eff_n_conc = 4

    log_level = _pick(log_level_str, f.log_level if f else None, "info")
    log_level_int = getattr(logging, log_level.upper(), logging.INFO)

    agent_eff = _pick(agent, f.agent if f else None, None)
    agent_import_eff = _pick(agent_import_path, f.agent_import_path if f else None, None)
    no_rebuild_eff = _pick(no_rebuild, f.no_rebuild if f else None, False)
    docker_image_strategy_eff = resolve_docker_image_strategy(
        no_rebuild=no_rebuild_eff,
        explicit_strategy=_pick(
            docker_image_strategy,
            f.docker_image_strategy if f else None,
            None,
        ),
    )

    return {
        "dataset_path": _pick(dataset_path, f.dataset_path if f else None, None),
        "output_path": _pick(output_path, f.output_path if f else None, Path("runs")),
        "run_id": _pick(run_id, f.run_id if f else None, None),
        "task_ids": _pick(task_ids, f.task_ids if f else None, None),
        "n_tasks": _pick(n_tasks, f.n_tasks if f else None, None),
        "exclude_task_ids": _pick(
            exclude_task_ids, f.exclude_task_ids if f else None, None
        ),
        "no_rebuild": no_rebuild_eff,
        "docker_image_strategy": docker_image_strategy_eff,
        "docker_image_namespace": _pick(
            docker_image_namespace,
            f.docker_image_namespace if f else None,
            None,
        ),
        "docker_image_tag": _pick(
            docker_image_tag,
            f.docker_image_tag if f else None,
            None,
        ),
        "cleanup": _pick(cleanup, f.cleanup if f else None, True),
        "model_name": eff_model,
        "agent": agent_eff,
        "agent_import_path": agent_import_eff,
        "log_level_int": log_level_int,
        "livestream": _pick(livestream, f.livestream if f else None, False),
        "n_concurrent_trials": eff_n_conc,
        "n_attempts": _pick(n_attempts, f.n_attempts if f else None, 1),
        "agent_kwargs": merge_agent_kwargs(file_kw, agent_kwargs_cli),
        "global_timeout_multiplier": _pick(
            global_timeout_multiplier,
            f.global_timeout_multiplier if f else None,
            1.0,
        ),
        "global_agent_timeout_sec": _pick(
            global_agent_timeout_sec,
            f.global_agent_timeout_sec if f else None,
            None,
        ),
        "global_test_timeout_sec": _pick(
            global_test_timeout_sec,
            f.global_test_timeout_sec if f else None,
            None,
        ),
    }
