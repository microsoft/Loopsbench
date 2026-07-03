"""Beautiful Rich-based reporting for Long-Horizon-Bench runs."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from long_horizon_bench.harness.models import BenchmarkResults, RunMetadata

console = Console()


def print_run_header(run_id: str, agent: str, model: str | None, n_tasks: int) -> None:
    """Print a styled header at the start of a run."""
    header = Table.grid(padding=(0, 2))
    header.add_column(style="bold cyan", justify="right")
    header.add_column()
    header.add_row("Run ID", run_id)
    header.add_row("Agent", agent)
    header.add_row("Model", model or "N/A")
    header.add_row("Tasks", str(n_tasks))

    console.print()
    console.print(
        Panel(
            header,
            title="[bold]Long-Horizon-Bench[/bold]",
            border_style="bright_blue",
            padding=(1, 2),
        )
    )
    console.print()


def print_results_summary(
    results: BenchmarkResults,
    output_path: Path,
) -> None:
    """Print a rich summary table after a run completes."""
    # Summary metrics table
    table = Table(
        title="Results Summary",
        show_header=True,
        header_style="bold magenta",
        border_style="bright_blue",
        padding=(0, 1),
    )
    table.add_column("Metric", style="cyan", min_width=20)
    table.add_column("Value", justify="right", min_width=15)

    table.add_row("Total Trials", str(len(results.results)))
    table.add_row(
        "Resolved",
        Text(str(results.n_resolved), style="bold green"),
    )
    table.add_row(
        "Unresolved",
        Text(str(results.n_unresolved), style="bold red"),
    )
    table.add_row(
        "Accuracy",
        Text(f"{results.accuracy:.2%}", style="bold yellow"),
    )

    for k, v in sorted(results.pass_at_k.items()):
        if v is not None:
            table.add_row(f"Pass@{k}", f"{v:.2%}")

    console.print()
    console.print(table)

    # Resolved / unresolved task lists
    if results.resolved_ids:
        resolved_text = ", ".join(sorted(results.resolved_ids))
        console.print(
            Panel(
                resolved_text,
                title=f"[green]Resolved Tasks ({results.n_resolved})[/green]",
                border_style="green",
            )
        )

    if results.unresolved_ids:
        unresolved_text = ", ".join(sorted(results.unresolved_ids))
        console.print(
            Panel(
                unresolved_text,
                title=f"[red]Unresolved Tasks ({results.n_unresolved})[/red]",
                border_style="red",
            )
        )

    # Token usage
    total_in = sum(r.total_input_tokens or 0 for r in results.results)
    total_out = sum(r.total_output_tokens or 0 for r in results.results)
    if total_in > 0 or total_out > 0:
        token_table = Table(
            title="Token Usage",
            show_header=True,
            header_style="bold blue",
            border_style="bright_blue",
        )
        token_table.add_column("Type", style="cyan")
        token_table.add_column("Total", justify="right")
        token_table.add_column("Avg / Trial", justify="right")

        n = max(len(results.results), 1)
        token_table.add_row("Input", f"{total_in:,}", f"{total_in // n:,}")
        token_table.add_row("Output", f"{total_out:,}", f"{total_out // n:,}")
        token_table.add_row(
            "Total",
            f"{total_in + total_out:,}",
            f"{(total_in + total_out) // n:,}",
        )
        console.print(token_table)

    # Failure modes
    failure_counts: dict[str, int] = {}
    for r in results.results:
        if not r.is_resolved and r.failure_mode:
            fm = r.failure_mode if isinstance(r.failure_mode, str) else r.failure_mode.value
            failure_counts[fm] = failure_counts.get(fm, 0) + 1

    if failure_counts:
        fm_table = Table(
            title="Failure Modes",
            show_header=True,
            header_style="bold red",
            border_style="red",
        )
        fm_table.add_column("Mode", style="red")
        fm_table.add_column("Count", justify="right")
        fm_table.add_column("% of Total", justify="right")

        for mode, count in sorted(
            failure_counts.items(), key=lambda x: x[1], reverse=True
        ):
            pct = count / max(len(results.results), 1) * 100
            fm_table.add_row(mode, str(count), f"{pct:.1f}%")

        console.print(fm_table)

    console.print()
    console.print(
        f"[bold green]Results written to[/bold green] {output_path.absolute()}"
    )
    console.print()


def print_run_status(
    run_id: str,
    passed: list[str],
    failed: list[str],
    incomplete: list[str],
    not_started: list[str],
) -> None:
    """Print a run status overview (for ``lhb runs status``)."""
    console.print(f"\n[bold]Run Status: {run_id}[/bold]")

    if passed:
        console.print(f"\n[bold green]Passed ({len(passed)}):[/bold green]")
        for t in sorted(passed):
            console.print(f"  [green]\u2705[/green] {t}")

    if failed:
        console.print(f"\n[bold red]Failed ({len(failed)}):[/bold red]")
        for t in sorted(failed):
            console.print(f"  [red]\u274c[/red] {t}")

    if incomplete:
        console.print(
            f"\n[bold yellow]Incomplete ({len(incomplete)}):[/bold yellow]"
        )
        for t in sorted(incomplete):
            console.print(f"  [yellow]\u23f3[/yellow] {t}")

    if not_started:
        console.print(
            f"\n[bold blue]Not Started ({len(not_started)}):[/bold blue]"
        )
        for t in sorted(not_started):
            console.print(f"  [blue]\u2b1c[/blue] {t}")

    total = len(passed) + len(failed) + len(incomplete) + len(not_started)
    done = len(passed) + len(failed)
    console.print(f"\n[bold]Summary:[/bold]")
    console.print(f"  Passed:      {len(passed)}")
    console.print(f"  Failed:      {len(failed)}")
    console.print(f"  Incomplete:  {len(incomplete)}")
    console.print(f"  Not Started: {len(not_started)}")
    console.print(f"  Total:       {total}")

    if total > 0:
        console.print(f"  Progress:    {done / total * 100:.1f}%")
        if done > 0:
            console.print(
                f"  Pass Rate:   {len(passed) / done * 100:.1f}% "
                f"({len(passed)}/{done})"
            )
    console.print()
