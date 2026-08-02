<h1 align="center">LoopsBench</h1>

<p align="center"><strong>A benchmark for evaluating AI agents on long-horizon terminal tasks.</strong></p>

<p align="center">
  <a href="https://loopsbench.ai"><img alt="Website" src="https://img.shields.io/badge/website-loopsbench.ai-0f766e?logo=googlechrome&logoColor=white"></a>
  <a href="https://loopsbench.ai/run"><img alt="API reference" src="https://img.shields.io/badge/API-reference-2563eb?logo=readthedocs&logoColor=white"></a>
  <a href="#citation"><img alt="arXiv coming soon" src="https://img.shields.io/badge/arXiv-coming%20soon-b31b1b.svg?logo=arxiv"></a>
  <a href="https://github.com/microsoft/Loopsbench/actions/workflows/pages.yml"><img alt="Pages" src="https://github.com/microsoft/Loopsbench/actions/workflows/pages.yml/badge.svg"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-3776ab?logo=python&logoColor=white">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MIT-green"></a>
</p>

<p align="center">
  <a href="https://loopsbench.ai">Website</a> .
  <a href="https://loopsbench.ai/quickstart/">Quick Start</a> .
  <a href="https://loopsbench.ai/run">API Docs</a> .
  <a href="CONTRIBUTING.md">Contribute Tasks</a>
</p>

---

## Introduction

LoopsBench is a benchmark and harness for measuring how well AI coding agents complete long-horizon software tasks in terminal environments. A LoopsBench task packages an agent-visible workspace, unit-level requirements, dependency graphs, Docker-backed execution, and verifiers that distinguish incomplete, partial, and complete solutions.

This repository is the source of truth for:

- the `loopsbench` Python package and Typer CLI
- benchmark task definitions under `tasks/`
- Docker image resolution and local/remote execution strategies
- built-in agent adapters and custom-agent loading
- GitHub-native task proposal, validation, and publish workflows
- the documentation site published at [loopsbench.ai](https://loopsbench.ai)

## Why LoopsBench

Many coding benchmarks emphasize isolated edits. LoopsBench focuses on tasks that require agents to plan, implement, test, and recover across multiple connected units. Each task can encode module dependencies, unit acceptance criteria, hidden or public tests, and an Oracle run used by maintainers to verify the task before publication.

Key properties:

- **Long-horizon structure**: tasks are decomposed into modules and separately testable units.
- **Terminal realism**: agents work inside task containers with command-line tools, logs, and test scripts.
- **Reproducible execution**: Docker-backed tasks support remote images, local builds, or existing local images.
- **Agent breadth**: built-in adapters include Oracle, Mini SWE-agent, SWE-agent, OpenHands, Claude Code, Cursor, Codex, Qwen Code, and Copilot.
- **Contribution safety**: untrusted task PRs are validated by trusted workflow code before merge.

## What's New

- GitHub-native task proposals and PR validation are now included in this repository.
- `loopsbench run` supports remote, local-build, and local-existing Docker image strategies.
- Task publishing emits deterministic bundles and SHA-256 checksums from trusted default-branch workflows.
- API documentation is generated automatically and deployed to [loopsbench.ai](https://loopsbench.ai).

## Installation

Install with `uv`:

```bash
uv sync --dev
uv run loopsbench --help
```

Or install in an editable virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
loopsbench --help
```

Module-style invocation is also supported:

```bash
python -m loopsbench.cli.main --help
```

## Quick Start

List the checked-in benchmark tasks:

```bash
loopsbench tasks list --tasks-dir tasks
```

Validate task metadata and required files:

```bash
loopsbench tasks validate --tasks-dir tasks
```

Run the example task with the Oracle agent and a local Docker build:

```bash
loopsbench run \
  --dataset-path tasks \
  --task-id task_tcp_course_stack \
  --agent oracle \
  --docker-image-strategy local-build
```

Run against prebuilt remote task images:

```bash
loopsbench run \
  --dataset-path tasks \
  --task-id task_tcp_course_stack \
  --agent codex \
  --model provider/model-name \
  --docker-image-strategy remote \
  --docker-image-namespace your-dockerhub-namespace \
  --docker-image-tag latest
```

## Configuration

Most `loopsbench run` options can be expressed in YAML and overridden from the CLI:

```yaml
dataset_path: tasks
output_path: runs
task_ids:
  - task_tcp_course_stack
agent: codex
model: provider/model-name
docker_image_strategy: local-build
n_concurrent: 2
n_attempts: 1
log_level: info
```

Run with:

```bash
loopsbench run --config run.yaml
```

Agent and model environment variables can be provided separately so secrets do not live in run configs:

```yaml
env_files:
  - .env
env:
  OPENAI_MODEL: gpt-5
```

Then pass it with:

```bash
loopsbench run --config run.yaml --model-config model-env.yaml
```

## Python API

The public Python API mirrors the CLI internals for datasets, task metadata, run configuration, Docker image strategy resolution, and agent loading:

```python
from pathlib import Path

from loopsbench.agents.agent_factory import AgentFactory
from loopsbench.agents.agent_name import AgentName
from loopsbench.dataset.dataset import Dataset
from loopsbench.task_images.strategy import DockerImageStrategy, resolve_task_docker_image

dataset = Dataset(path=Path("tasks"), task_ids=["task_tcp_course_stack"])
print(dataset.task_ids)

oracle = AgentFactory.get_agent(AgentName.ORACLE)
image = resolve_task_docker_image(
    task_id="task_tcp_course_stack",
    strategy=DockerImageStrategy.LOCAL_BUILD,
    docker_image_namespace=None,
    docker_image_tag=None,
)
print(oracle.name(), image.client_image_ref)
```

See the generated [API reference](https://loopsbench.ai/run) for module-level documentation.

## Submit a Task

LoopsBench uses a GitHub-native contribution flow. Contributors should not upload ZIP archives or finished task bundles through the website. Proposals and task implementations are reviewed in GitHub.

1. Read [CONTRIBUTING.md](CONTRIBUTING.md), `tasks/_template/`, and `tasks/task_tcp_course_stack/`.
2. Open a Task Proposal issue with `.github/ISSUE_TEMPLATE/task-proposal.yml`.
3. Wait for maintainer approval.
4. Build the task in your fork under `tasks/task_<id>/`.
5. Open a pull request with `.github/pull_request_template.md`.
6. Pass static validation, Oracle validation, and maintainer review.

Create a new task from the template:

```bash
loopsbench tasks create task_my_new_case
```

Run local validation before opening a PR:

```bash
python3 scripts/validate_task_contribution.py --task-dir tasks/task_my_new_case --static-only
loopsbench tasks validate --task-id task_my_new_case
loopsbench run --agent oracle --task-id task_my_new_case --dataset-path tasks --docker-image-strategy local-build
```

## Repository Layout

```text
LoopsBench/
  .github/       # Issue forms, PR template, validation and publish workflows
  docs/          # Documentation site and generated API reference sources
  loopsbench/    # Core package, CLI, harness, Docker helpers, agent adapters
  scripts/       # Contribution validation, publish, and repo admin helpers
  tasks/         # Source-controlled benchmark tasks and the task template
  tests/         # Repository-level tests
  mkdocs.yml     # GitHub Pages documentation configuration
  pyproject.toml
  registry.json
  uv.lock
```

## Testing

Run the focused repository tests:

```bash
python -m pytest \
  tests/test_docker_compose_manager.py \
  tests/test_harness_docker_image_metadata.py \
  tests/test_repo_profiles.py \
  tests/test_run_docker_image_strategy.py -q
```

Run the contribution-flow tests:

```bash
python -m pytest \
  tests/test_agent_factory.py \
  tests/test_pr_proposal_check.py \
  tests/test_task_contribution_validation.py \
  tests/test_list_changed_tasks.py \
  tests/test_list_pr_changed_files.py \
  tests/test_task_pr_path_check.py \
  tests/test_publish_workflow_helpers.py -q
```

Build the documentation locally:

```bash
python -m pip install -r docs/requirements.txt
python -m pip install -e .
mkdocs serve
```

## Citation

The LoopsBench paper is coming soon. For now, please cite the repository URL and version or commit SHA used in your experiments.

```bibtex
@misc{loopsbench2026,
  title        = {LoopsBench: A Benchmark for Long-Horizon Terminal Agents},
  howpublished = {\url{https://github.com/microsoft/Loopsbench}},
  year         = {2026},
  note         = {arXiv coming soon}
}
```

## License

This project is licensed under the [MIT License](LICENSE). Third-party agent integrations and vendored components may carry their own licenses; review their local license files before redistribution.
