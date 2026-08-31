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

The Oracle agent applies the task solution and is useful for validating task packaging:

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

If the task images are already published to a registry, switch to `remote` instead of `local-build`. The code requires `--docker-image-namespace` when `--docker-image-strategy remote` is used; the current public Docker Hub namespace is `dolischwer`, and `--docker-image-tag` defaults to `latest`.

```bash
loopsbench run \
  --dataset-path tasks \
  --task-id task_tcp_course_stack \
  --agent codex \
  --model provider/model-name \
  --model-config model-env.yaml \
  --docker-image-strategy remote \
  --docker-image-namespace dolischwer \
  --docker-image-tag latest
```

## Run a Preinstalled External Agent

Use `external-command` when the task image already contains a solver command.
LoopsBench writes a versioned request inside the task container, invokes the
configured argv with shell-escaped arguments, collects its result receipt, and then
continues with the normal verifier and scoring flow.

```bash
loopsbench run \
  --dataset-path tasks \
  --task-id task_tcp_course_stack \
  --agent external-command \
  --agent-kwarg 'command=["loopx","benchmark","agent-phase","--execute","--solver-command-json","[\"solver\",\"--mode\",\"task\"]"]' \
  --docker-image-strategy local-build
```

The external command receives `LOOPSBENCH_EXTERNAL_AGENT_REQUEST`,
`LOOPSBENCH_EXTERNAL_AGENT_RESULT`, `LOOPSBENCH_EXTERNAL_AGENT_WORKSPACE`, and
`LOOPSBENCH_EXTERNAL_AGENT_TIMEOUT_SECONDS`. It must write the
`external_agent_result_v1` result to the supplied result path. This interface
does not give the command verifier, score, submission, or task-provisioning
authority.

`external-command` treats the task container as the runner-owned containment
boundary. Before launch it rejects privileged containers, shared PID namespaces,
and Docker-socket mounts. After normal completion or timeout, LoopsBench restarts
the task container and verifies a new container generation before reading the
result or starting the verifier. Workspace files persist across this drain, but
shell state and background processes do not. If the drain cannot be verified, the
trial records `agent_safety_error` and the verifier does not start.

## Review Results

By default, run artifacts are written under `runs/<run-id>/`. Use:

```bash
loopsbench runs list --runs-dir runs
loopsbench runs status --run-id <run-id> --runs-dir runs
loopsbench runs summarize --run-id <run-id> --runs-dir runs
```
