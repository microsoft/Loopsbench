"""CLI commands for managing tasks."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from typer import Option, Typer

from loopsbench.handlers.trial_handler import Task
from loopsbench.utils.compose_security import validate_compose_security

tasks_app = Typer(no_args_is_help=True)
console = Console()


# ---------------------------------------------------------------------------
# loopsbench tasks list
# ---------------------------------------------------------------------------


@tasks_app.command("list")
def list_tasks(
    tasks_dir: Annotated[Path, Option(help="Path to tasks directory")] = Path("tasks"),
):
    """List all tasks with their metadata."""
    if not tasks_dir.exists():
        console.print(f"[red]Tasks directory {tasks_dir} does not exist.[/red]")
        raise typer.Exit(1)

    table = Table(
        title="Available Tasks",
        show_header=True,
        header_style="bold cyan",
        border_style="bright_blue",
    )
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Task ID", style="bold")
    table.add_column("Difficulty")
    table.add_column("Category")
    table.add_column("Agent Timeout", justify="right")
    table.add_column("Author")

    count = 0
    for task_path in sorted(tasks_dir.iterdir()):
        if not task_path.is_dir() or task_path.name.startswith("_"):
            continue

        config_path = task_path / "task.yaml"
        if not config_path.exists():
            continue

        try:
            task = Task.from_yaml(config_path)
            count += 1

            diff_style = {
                "easy": "green",
                "medium": "yellow",
                "hard": "red",
            }.get(task.difficulty.value, "white")

            table.add_row(
                str(count),
                task_path.name,
                f"[{diff_style}]{task.difficulty.value}[/{diff_style}]",
                task.category,
                f"{task.max_agent_timeout_sec:.0f}s",
                task.author_name,
            )
        except Exception as exc:
            count += 1
            table.add_row(
                str(count),
                task_path.name,
                "[red]ERROR[/red]",
                str(exc)[:40],
                "",
                "",
            )

    console.print(table)
    console.print(f"\n[dim]Total: {count} tasks[/dim]")


# ---------------------------------------------------------------------------
# loopsbench tasks validate
# ---------------------------------------------------------------------------


@tasks_app.command()
def validate(
    tasks_dir: Annotated[Path, Option(help="Path to tasks directory")] = Path("tasks"),
    task_id: Annotated[
        str | None, Option("-t", "--task-id", help="Validate a single task")
    ] = None,
):
    """Validate task files for correctness and completeness."""
    if not tasks_dir.exists():
        console.print(f"[red]Tasks directory {tasks_dir} does not exist.[/red]")
        raise typer.Exit(1)

    if task_id:
        task_dirs = [tasks_dir / task_id]
    else:
        task_dirs = sorted(
            p for p in tasks_dir.iterdir() if p.is_dir() and not p.name.startswith("_")
        )

    required_files = [
        "task.yaml",
        "Dockerfile",
        "docker-compose.yaml",
        "run-tests.sh",
        "unit_dag.json",
        "module_dag.yaml",
        "slug_diff_map.json",
        "solution.sh",
    ]

    errors = 0
    for td in task_dirs:
        task_name = td.name
        issues: list[str] = []

        if not td.exists():
            issues.append("Task directory does not exist")
            errors += 1
            console.print(f"[red]\u274c {task_name}[/red]")
            for issue in issues:
                console.print(f"    {issue}")
            continue

        # Check required files
        for fname in required_files:
            if not (td / fname).exists():
                # Allow solution.yaml as alternative to solution.sh
                if fname == "solution.sh" and (td / "solution.yaml").exists():
                    continue
                issues.append(f"Missing: {fname}")

        # Validate task.yaml
        config = td / "task.yaml"
        if config.exists():
            try:
                task = Task.from_yaml(config)
                if not task.instruction.strip():
                    issues.append("Empty instruction in task.yaml")
                if task.author_name == "unknown":
                    issues.append("Missing author_name in task.yaml")
                if task.author_email == "unknown":
                    issues.append("Missing author_email in task.yaml")
                if task.difficulty.value not in ("easy", "medium", "hard"):
                    issues.append(f"Invalid difficulty: {task.difficulty.value}")
                if not task.parser_name:
                    issues.append("Missing parser_name in task.yaml")

                compose_file = Path(task.docker.compose_file)
                if compose_file.is_absolute() or compose_file.name == "":
                    issues.append("docker.compose_file must be a relative path")
                else:
                    compose_path = (td / compose_file).resolve()
                    task_root = td.resolve()
                    if (
                        task_root not in compose_path.parents
                        and compose_path != task_root
                    ):
                        issues.append(
                            "docker.compose_file must stay within the task directory"
                        )
                    elif not compose_path.exists():
                        issues.append(
                            f"Missing docker compose file: {task.docker.compose_file}"
                        )
                    else:
                        for security_issue in validate_compose_security(compose_path):
                            issues.append(security_issue.message)
            except Exception as exc:
                issues.append(f"Invalid task.yaml: {exc}")

        # Check tests directory
        test_dir = td / "tests"
        if not test_dir.exists() or not list(test_dir.iterdir()):
            issues.append("Missing or empty tests/ directory")

        base_dir = td / "base"
        if not base_dir.exists():
            issues.append("Missing base/ directory")

        requirements_dir = td / "requirements"
        if not requirements_dir.exists() or not list(requirements_dir.glob("*.yaml")):
            issues.append("Missing or empty requirements/ directory")

        if issues:
            errors += 1
            console.print(f"[red]\u274c {task_name}[/red]")
            for issue in issues:
                console.print(f"    {issue}")
        else:
            console.print(f"[green]\u2705 {task_name}[/green]")

    console.print()
    if errors:
        console.print(f"[bold red]{errors} task(s) have validation errors.[/bold red]")
        raise typer.Exit(1)
    else:
        console.print(
            f"[bold green]All {len(task_dirs)} task(s) passed validation.[/bold green]"
        )


# ---------------------------------------------------------------------------
# loopsbench tasks create
# ---------------------------------------------------------------------------


@tasks_app.command()
def create(
    task_id: Annotated[str, typer.Argument(help="Name/ID for the new task")],
    tasks_dir: Annotated[Path, Option(help="Path to tasks directory")] = Path("tasks"),
):
    """Create a new task from the template."""
    template_dir = tasks_dir / "_template"
    target_dir = tasks_dir / task_id

    if target_dir.exists():
        console.print(f"[red]Task directory {target_dir} already exists.[/red]")
        raise typer.Exit(1)

    if not template_dir.exists():
        console.print(f"[red]Template directory {template_dir} not found.[/red]")
        raise typer.Exit(1)

    shutil.copytree(template_dir, target_dir)
    console.print(
        f"[green]Created new task at {target_dir}[/green]\n"
        f"Edit the files in {target_dir}/ to define your task."
    )
