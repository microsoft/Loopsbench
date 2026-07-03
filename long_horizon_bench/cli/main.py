"""Long-Horizon-Bench CLI entry point."""

from dotenv import load_dotenv
from typer import Typer

from long_horizon_bench.cli.runs import create, runs_app
from long_horizon_bench.cli.tasks import tasks_app

load_dotenv()

app = Typer(
    name="lhb",
    no_args_is_help=True,
    help="Long-Horizon-Bench: evaluate AI agents on long-horizon tasks.",
)
app.add_typer(tasks_app, name="tasks", help="Manage tasks.")
app.add_typer(runs_app, name="runs", help="Manage runs.")

# Shortcut: `lhb run ...` == `lhb runs create ...`
app.command(
    name="run",
    help="Run the Long-Horizon-Bench harness (alias for `lhb runs create`).",
)(create)

if __name__ == "__main__":
    app()
