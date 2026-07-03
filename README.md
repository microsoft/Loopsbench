# LoopsBench

`LoopsBench` is currently a trimmed-down working copy of the Long-Horizon-Bench core codebase.

This repository contains:
- the Python package under `long_horizon_bench/`
- repository-level tests under `tests/`
- utility and validation scripts under `scripts/`
- packaging metadata such as `pyproject.toml`, `uv.lock`, and `registry.json`

This repository does **not** currently include the full benchmark dataset checkout. In particular, there is no top-level `tasks/`, `docs/`, `examples/`, `runs/`, or other benchmark asset directories in this copy.

## Current Layout

```text
LoopsBench/
  long_horizon_bench/   # Core package and CLI
  scripts/              # Validation, image, and maintenance scripts
  tests/                # Repository-level Python tests
  pyproject.toml        # Package metadata
  registry.json         # Registry metadata
  uv.lock               # Locked Python dependencies
  README.md
```

## Naming

The repository folder has been renamed to `LoopsBench`, but the packaged Python module and CLI entry points still use the upstream names:
- Python package: `long_horizon_bench`
- CLI commands: `lhb` and `long-horizon-bench`

That is why the source tree and command examples below still use `long_horizon_bench` and `lhb`.

## Setup

Install the package in editable mode with either `uv` or `pip`:

```bash
uv sync --dev
```

or:

```bash
pip install -e .
```

## What Works In This Copy

This checkout is useful for:
- editing and reading the harness, CLI, agent integrations, parsers, and helper utilities
- running self-contained repository tests
- working on the scripts themselves

Examples:

```bash
pytest tests/test_copilot_agent.py
pytest tests/test_docker_compose_manager.py
pytest tests/task_images/test_registry.py
python3 scripts/generate_nonseg_image_manifest.py --help
python3 scripts/validate_per_pr.py --help
```

## Missing Benchmark Data

Many commands in the codebase still assume the original benchmark layout and look for a top-level `tasks/` directory.

In this trimmed repository:
- `lhb tasks ...` and `lhb run ...` are still available, but they need an external dataset/task directory to be useful
- several scripts under `scripts/` default to `tasks/` paths that are not present here
- some tests under `tests/` also expect task fixtures from the original benchmark checkout

Examples of task-dependent tests in this copy include:
- `tests/test_validate_per_pr_alignment.py`
- `tests/test_validate_per_pr_paper_end_nodes.py`
- `tests/test_validate_per_pr_task_metadata_alignment.py`
- `tests/test_task_hadoop_seg05_gold_patch_regressions.py`
- `tests/test_task_hadoop_seg05_runner_selection.py`
- `tests/test_rdma_paper_checks_helper.py`

If you want to use those scripts or tests, add or mount a compatible `tasks/` tree beside this repository, or port the relevant task assets into this checkout.

## CLI Notes

The CLI entry point is still the upstream harness CLI:

```bash
lhb --help
lhb runs --help
lhb tasks --help
```

Without a benchmark dataset checkout, the CLI is best treated here as package code to develop and inspect rather than a fully runnable benchmark distribution.
