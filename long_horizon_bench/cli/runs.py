"""CLI commands for managing harness runs."""

from __future__ import annotations

import ast
import logging
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from typer import Option, Typer

from long_horizon_bench.agents.agent_name import AgentName
from long_horizon_bench.file_config import (
    apply_model_runtime_config,
    load_model_runtime_file,
    load_run_file,
    resolve_run_params,
)
from long_horizon_bench.harness.models import BenchmarkResults, RunMetadata, TrialResults
from long_horizon_bench.task_images.strategy import (
    DockerImageStrategy,
    validate_remote_docker_image_coordinates,
)

runs_app = Typer(no_args_is_help=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class HelpPanel(str, Enum):
    DATASET = "Dataset"
    OUTPUT = "Output"
    TASKS = "Tasks"
    BUILD = "Build"
    AGENT = "Agent"
    LOGGING = "Logging"
    CONCURRENCY = "Concurrency"
    TIMEOUT = "Timeout"


class LogLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


def _infer_type(value: str):
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return value


def _agent_kwarg_cli_list_to_dict(agent_kwargs: list[str]) -> dict:
    processed: dict = {}
    for kw in agent_kwargs:
        key, value = kw.split("=", 1)
        processed[key] = _infer_type(value)
    return processed


def _build_agent_kwargs_for_harness(
    merged_from_file_and_cli: dict,
    no_rebuild: bool,
    model_name: str | None,
) -> dict:
    processed = dict(merged_from_file_and_cli)
    processed["no_rebuild"] = no_rebuild
    if model_name is not None:
        processed["model_name"] = model_name
    return processed


def deprecated_no_rebuild_warning(no_rebuild: bool | None) -> str | None:
    if not no_rebuild:
        return None
    return (
        "Warning: --no-rebuild is deprecated and retained only as a compatibility "
        "alias for --docker-image-strategy local-existing. Prefer "
        "--docker-image-strategy local-existing."
    )


# ---------------------------------------------------------------------------
# lhb run / lhb runs create
# ---------------------------------------------------------------------------

def create(
    config: Annotated[
        Path | None,
        Option(
            "-c",
            "--config",
            help="YAML file with run options (CLI overrides file).",
            rich_help_panel=HelpPanel.DATASET,
        ),
    ] = None,
    model_config: Annotated[
        Path | None,
        Option(
            "--model-config",
            help="YAML file with agent/model env (env_files + env map).",
            rich_help_panel=HelpPanel.AGENT,
        ),
    ] = None,
    dataset_path: Annotated[
        Path | None,
        Option(
            "-p", "--dataset-path",
            help="Path to the tasks directory",
            rich_help_panel=HelpPanel.DATASET,
            show_default="tasks/",
        ),
    ] = None,
    output_path: Annotated[
        Path | None,
        Option(help="Path to output directory", rich_help_panel=HelpPanel.OUTPUT),
    ] = None,
    run_id: Annotated[
        str | None,
        Option(
            help="Unique identifier for this run",
            rich_help_panel=HelpPanel.OUTPUT,
            show_default="YYYY-MM-DD__HH-MM-SS",
        ),
    ] = None,
    task_ids: Annotated[
        list[str] | None,
        Option(
            "-t", "--task-id",
            help="Task IDs or glob patterns to run (repeatable).",
            rich_help_panel=HelpPanel.TASKS,
            show_default="All tasks",
        ),
    ] = None,
    n_tasks: Annotated[
        int | None,
        Option(
            help="Maximum number of tasks to run",
            rich_help_panel=HelpPanel.TASKS,
        ),
    ] = None,
    exclude_task_ids: Annotated[
        list[str] | None,
        Option(
            "-e", "--exclude-task-id",
            help="Task IDs or patterns to exclude (repeatable).",
            rich_help_panel=HelpPanel.TASKS,
        ),
    ] = None,
    no_rebuild: Annotated[
        bool | None,
        Option(
            "--no-rebuild/--rebuild",
            help=(
                "Deprecated compatibility alias for "
                "--docker-image-strategy local-existing."
            ),
            rich_help_panel=HelpPanel.BUILD,
        ),
    ] = None,
    docker_image_strategy: Annotated[
        DockerImageStrategy | None,
        Option(
            "--docker-image-strategy",
            help="Docker image strategy: remote, local-build, or local-existing.",
            rich_help_panel=HelpPanel.BUILD,
        ),
    ] = None,
    docker_image_namespace: Annotated[
        str | None,
        Option(
            "--docker-image-namespace",
            help="Docker Hub namespace for remote task images.",
            rich_help_panel=HelpPanel.BUILD,
        ),
    ] = None,
    docker_image_tag: Annotated[
        str | None,
        Option(
            "--docker-image-tag",
            help="Docker Hub tag for remote task images.",
            rich_help_panel=HelpPanel.BUILD,
        ),
    ] = None,
    cleanup: Annotated[
        bool | None,
        Option(
            "--cleanup/--no-cleanup",
            help="Remove Docker images after run",
            rich_help_panel=HelpPanel.BUILD,
        ),
    ] = None,
    model_name: Annotated[
        str | None,
        Option(
            "-m", "--model",
            help="Model name (provider/model_name)",
            rich_help_panel=HelpPanel.AGENT,
        ),
    ] = None,
    agent: Annotated[
        AgentName | None,
        Option(
            "-a", "--agent",
            help="Built-in agent to use.",
            rich_help_panel=HelpPanel.AGENT,
            show_default=AgentName.ORACLE.value,
        ),
    ] = None,
    agent_import_path: Annotated[
        str | None,
        Option(
            help="Import path for a custom agent (module.path:ClassName).",
            rich_help_panel=HelpPanel.AGENT,
        ),
    ] = None,
    log_level: Annotated[
        LogLevel | None,
        Option(help="Logging level", rich_help_panel=HelpPanel.LOGGING),
    ] = None,
    livestream: Annotated[
        bool | None,
        Option(
            "--livestream/--no-livestream",
            help="Enable livestreaming",
            rich_help_panel=HelpPanel.LOGGING,
        ),
    ] = None,
    n_concurrent_trials: Annotated[
        int | None,
        Option(
            "--n-concurrent",
            help="Number of concurrent trials",
            rich_help_panel=HelpPanel.CONCURRENCY,
        ),
    ] = None,
    n_attempts: Annotated[
        int | None,
        Option(
            help="Number of attempts per task",
            rich_help_panel=HelpPanel.CONCURRENCY,
        ),
    ] = None,
    agent_kwargs: Annotated[
        list[str],
        Option(
            "-k", "--agent-kwarg",
            help="Additional agent kwarg (key=value, repeatable).",
            rich_help_panel=HelpPanel.AGENT,
        ),
    ] = [],
    global_timeout_multiplier: Annotated[
        float | None,
        Option(
            help="Multiplier for all timeouts",
            rich_help_panel=HelpPanel.TIMEOUT,
        ),
    ] = None,
    global_agent_timeout_sec: Annotated[
        float | None,
        Option(
            help="Override agent timeout (seconds)",
            rich_help_panel=HelpPanel.TIMEOUT,
        ),
    ] = None,
    global_test_timeout_sec: Annotated[
        float | None,
        Option(
            help="Override test timeout (seconds)",
            rich_help_panel=HelpPanel.TIMEOUT,
        ),
    ] = None,
):
    """Run the Long-Horizon-Bench harness with the specified configuration."""
    from long_horizon_bench.harness.harness import Harness
    from long_horizon_bench.reporters.rich_reporter import (
        console,
        print_results_summary,
        print_run_header,
    )

    run_file = load_run_file(config) if config is not None else None

    model_yaml_path = model_config
    if model_yaml_path is None and run_file and run_file.model_config_path is not None:
        model_yaml_path = run_file.model_config_path
    if model_yaml_path is not None:
        apply_model_runtime_config(load_model_runtime_file(model_yaml_path))

    log_level_str = log_level.value if log_level is not None else None

    try:
        rp = resolve_run_params(
            run_file,
            dataset_path=dataset_path,
            output_path=output_path,
            run_id=run_id,
            task_ids=task_ids,
            n_tasks=n_tasks,
            exclude_task_ids=exclude_task_ids,
            no_rebuild=no_rebuild,
            docker_image_strategy=docker_image_strategy,
            docker_image_namespace=docker_image_namespace,
            docker_image_tag=docker_image_tag,
            cleanup=cleanup,
            model_name=model_name,
            agent=agent,
            agent_import_path=agent_import_path,
            log_level_str=log_level_str,
            livestream=livestream,
            n_concurrent_trials=n_concurrent_trials,
            n_attempts=n_attempts,
            agent_kwargs_cli=_agent_kwarg_cli_list_to_dict(agent_kwargs),
            global_timeout_multiplier=global_timeout_multiplier,
            global_agent_timeout_sec=global_agent_timeout_sec,
            global_test_timeout_sec=global_test_timeout_sec,
        )
        validate_remote_docker_image_coordinates(
            strategy=rp["docker_image_strategy"],
            docker_image_namespace=rp["docker_image_namespace"],
            docker_image_tag=rp["docker_image_tag"],
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    no_rebuild_warning = deprecated_no_rebuild_warning(rp["no_rebuild"])
    if no_rebuild_warning is not None:
        console.print(f"[yellow]{no_rebuild_warning}[/yellow]")

    dataset_path_resolved: Path | None = rp["dataset_path"]
    if dataset_path_resolved is None:
        console.print(
            "[yellow]Note: No dataset path specified. "
            "Defaulting to tasks/ directory.[/yellow]"
        )
        dataset_path_resolved = Path("tasks")

    run_id_resolved: str | None = rp["run_id"]
    if run_id_resolved is None:
        run_id_resolved = datetime.now().strftime("%Y-%m-%d__%H-%M-%S")

    agent_resolved: AgentName | None = rp["agent"]
    agent_import_resolved: str | None = rp["agent_import_path"]
    if agent_resolved is None and agent_import_resolved is None:
        agent_resolved = AgentName.ORACLE

    if agent_resolved is not None and agent_import_resolved is not None:
        raise typer.BadParameter(
            "Cannot specify both --agent and --agent-import-path"
        )

    model_name_resolved: str | None = rp["model_name"]
    processed = _build_agent_kwargs_for_harness(
        rp["agent_kwargs"],
        rp["no_rebuild"],
        model_name_resolved,
    )
    model_str = (
        "Oracle" if agent_resolved == AgentName.ORACLE else model_name_resolved
    )

    harness = Harness(
        dataset_path=dataset_path_resolved,
        output_path=rp["output_path"],
        run_id=run_id_resolved,
        agent_name=agent_resolved,
        agent_import_path=agent_import_resolved,
        model_name=model_str,
        agent_kwargs=processed,
        no_rebuild=rp["no_rebuild"],
        docker_image_strategy=rp["docker_image_strategy"],
        docker_image_namespace=rp["docker_image_namespace"],
        docker_image_tag=rp["docker_image_tag"],
        cleanup=rp["cleanup"],
        log_level=rp["log_level_int"],
        task_ids=rp["task_ids"],
        n_tasks=rp["n_tasks"],
        livestream=rp["livestream"],
        n_concurrent_trials=rp["n_concurrent_trials"],
        exclude_task_ids=rp["exclude_task_ids"],
        n_attempts=rp["n_attempts"],
        global_timeout_multiplier=rp["global_timeout_multiplier"],
        global_agent_timeout_sec=rp["global_agent_timeout_sec"],
        global_test_timeout_sec=rp["global_test_timeout_sec"],
    )

    print_run_header(
        run_id=run_id_resolved,
        agent=harness._agent_class.name(),
        model=model_str,
        n_tasks=len(harness._dataset),
    )

    results = harness.run()
    print_results_summary(results, harness._results_output_path)


# ---------------------------------------------------------------------------
# lhb runs list
# ---------------------------------------------------------------------------

@runs_app.command("list")
def list_runs(
    runs_dir: Annotated[
        Path, Option(help="Path to runs directory")
    ] = Path("runs"),
):
    """List all runs in the runs directory."""
    from long_horizon_bench.reporters.rich_reporter import console

    if not runs_dir.exists():
        console.print(f"[red]Runs directory {runs_dir} does not exist.[/red]")
        raise typer.Exit(1)

    for run_dir in sorted(runs_dir.iterdir()):
        if run_dir.is_dir():
            console.print(run_dir.name)


# ---------------------------------------------------------------------------
# lhb runs status
# ---------------------------------------------------------------------------

@runs_app.command()
def status(
    run_id: Annotated[str, Option(help="Run ID to check status for")],
    runs_dir: Annotated[
        Path, Option(help="Path to runs directory")
    ] = Path("runs"),
):
    """Check the status of a specific run."""
    from long_horizon_bench.reporters.rich_reporter import console, print_run_status

    run_path = runs_dir / run_id
    if not run_path.exists():
        console.print(f"[red]Run directory {run_path} does not exist.[/red]")
        raise typer.Exit(1)

    metadata_path = run_path / "run_metadata.json"
    if not metadata_path.exists():
        console.print(f"[red]Metadata not found for run {run_id}.[/red]")
        raise typer.Exit(1)

    metadata = RunMetadata.model_validate_json(metadata_path.read_text())
    expected_ids = set(metadata.task_ids or [])

    if not expected_ids:
        console.print("[yellow]No task IDs found in metadata.[/yellow]")
        return

    n_attempts = metadata.n_attempts
    passed, failed, incomplete, not_started = [], [], [], []

    results_path = run_path / "results.json"
    completed_results: list[TrialResults] = []
    if results_path.exists():
        try:
            completed_results = BenchmarkResults.model_validate_json(
                results_path.read_text()
            ).results
        except Exception:
            completed_results = []

    results_by_task: dict[str, list[TrialResults]] = {}
    for result in completed_results:
        results_by_task.setdefault(result.task_id, []).append(result)

    for task_id in expected_ids:
        task_dir = run_path / task_id
        trial_results_list = results_by_task.get(task_id, [])
        completed = len(trial_results_list)

        if completed == 0 and not task_dir.exists():
            not_started.append(task_id)
            continue

        if completed >= n_attempts:
            if all(tr.is_resolved for tr in trial_results_list):
                passed.append(task_id)
            else:
                failed.append(task_id)
        else:
            incomplete.append(task_id)

    print_run_status(run_id, passed, failed, incomplete, not_started)


# ---------------------------------------------------------------------------
# lhb runs summarize
# ---------------------------------------------------------------------------

@runs_app.command()
def summarize(
    run_id: Annotated[str, Option(help="Run ID")],
    runs_dir: Annotated[
        Path, Option(help="Path to runs directory")
    ] = Path("runs"),
):
    """Summarize results for a specific run."""
    from long_horizon_bench.reporters.rich_reporter import (
        console,
        print_results_summary,
    )

    run_path = runs_dir / run_id
    results_path = run_path / "results.json"

    if not results_path.exists():
        console.print(f"[red]Results not found for run {run_id}.[/red]")
        raise typer.Exit(1)

    results = BenchmarkResults.model_validate_json(results_path.read_text())
    print_results_summary(results, results_path)


# Register create under runs_app as well
runs_app.command("create")(create)
