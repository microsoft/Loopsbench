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

## Get Tasks from a Release

Download the latest published task snapshot from GitHub Releases, verify the checksum, and extract it. The archive materializes a local `tasks/` directory, which is the dataset path used in the commands below:

```bash
mkdir -p loopsbench-release
cd loopsbench-release
# Or download the same three files from the latest GitHub release page.
gh release download --repo microsoft/Loopsbench \
  --pattern 'loopsbench-tasks-*.tar.zst' \
  --pattern 'loopsbench-tasks-*.tar.zst.sha256' \
  --pattern 'loopsbench-tasks-*.manifest.json'
sha256sum -c loopsbench-tasks-*.tar.zst.sha256
tar --zstd -xf loopsbench-tasks-*.tar.zst
# The archive creates ./tasks.
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

The Oracle agent applies the task solution and is useful for validating task packaging. `local-build` is the default and recommended image strategy for benchmark runs; the explicit flag below can be omitted:

```bash
loopsbench run \
  --dataset-path tasks \
  --task-id task_tcp_course_stack \
  --agent oracle \
  --docker-image-strategy local-build
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
  --model-config model-env.yaml \
  --docker-image-strategy local-build
```

## Use Remote Task Images

For tasks with published images, explicitly select `remote`. It defaults to the Docker Hub namespace `loopsbench` and tag `latest`, for example `loopsbench/loopsbench-task-tcp-course-stack:latest`. Use `--docker-image-namespace` or `--docker-image-tag` to override these defaults.

```bash
loopsbench run \
  --dataset-path tasks \
  --task-id task_tcp_course_stack \
  --agent codex \
  --model provider/model-name \
  --model-config model-env.yaml \
  --docker-image-strategy remote \
  --docker-image-namespace loopsbench \
  --docker-image-tag latest
```

## Review Results

By default, run artifacts are written under `runs/<run-id>/`. Use:

```bash
loopsbench runs list --runs-dir runs
loopsbench runs status --run-id <run-id> --runs-dir runs
loopsbench runs summarize --run-id <run-id> --runs-dir runs
```
