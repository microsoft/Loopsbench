# Quick Start

## Requirements

- Python 3.12 or newer
- Docker, for Docker-backed harness runs
- A model runtime configuration for non-Oracle agents

## Install

Use `uv` for the full development environment:

```bash
uv sync --dev
uv run loopsbench --help
```

Or install the package in editable mode:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
loopsbench --help
```

## Download Tasks

Download the task bundle from [Hugging Face](https://huggingface.co/datasets/LoopsBench/LoopsBench). The commands below verify the archive and extract it into `tasks/`, ready for the harness:

```bash
python -m pip install huggingface_hub
mkdir -p loopsbench-data
cd loopsbench-data
hf download LoopsBench/LoopsBench --repo-type dataset \
  --include 'loopsbench-tasks-*' --local-dir .
sha256sum -c loopsbench-tasks-*.tar.zst.sha256
tar --zstd -xf loopsbench-tasks-*.tar.zst
```

## Inspect Tasks

List tasks from the local `tasks/` dataset directory:

```bash
loopsbench tasks list --tasks-dir tasks
```

Validate task manifests and required files:

```bash
loopsbench tasks validate --tasks-dir tasks
```

## Run the Oracle Baseline

Oracle applies the reference solution to check that a task runs correctly. LoopsBench builds the task image locally by default:

```bash
loopsbench run \
  --dataset-path tasks \
  --task-id task_tcp_course_stack \
  --agent oracle
```

## Run a Model-Backed Agent

Provide model credentials through a model runtime file instead of hard-coding secrets in run configs:

```yaml
env_files:
  - .env
env:
  OPENAI_MODEL: gpt-5
```

Then run an agent:

```bash
loopsbench run \
  --dataset-path tasks \
  --task-id task_tcp_course_stack \
  --agent codex \
  --model provider/model-name \
  --model-config model-env.yaml
```

## Use Remote Task Images

To run a task with a published image, select `remote`. Images are pulled from the `loopsbench` Docker Hub namespace with the `latest` tag:

```bash
loopsbench run \
  --dataset-path tasks \
  --task-id task_tcp_course_stack \
  --agent codex \
  --model provider/model-name \
  --model-config model-env.yaml \
  --docker-image-strategy remote
```

See [Docker image strategies](guides/running.md#docker-image-strategies) for custom namespaces and tags.

## Review Results

By default, run artifacts are written under `runs/<run-id>/`. Use:

```bash
loopsbench runs list --runs-dir runs
loopsbench runs status --run-id <run-id> --runs-dir runs
loopsbench runs summarize --run-id <run-id> --runs-dir runs
```
