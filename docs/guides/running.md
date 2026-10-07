# Running Benchmarks

## Command Surface

`loopsbench run` is an alias for `loopsbench runs create`:

```bash
loopsbench run [OPTIONS]
loopsbench runs create [OPTIONS]
```

Use `loopsbench runs list`, `loopsbench runs status`, and `loopsbench runs summarize` to inspect completed or in-progress runs.

## Selecting Tasks

Run all tasks from a dataset directory:

```bash
loopsbench run --dataset-path tasks --docker-image-strategy local-build
```

Run one or more task IDs or glob patterns:

```bash
loopsbench run \
  --dataset-path tasks \
  --task-id task_tcp_course_stack \
  --task-id 'task_api_*' \
  --docker-image-strategy local-build
```

Exclude tasks with repeatable patterns:

```bash
loopsbench run \
  --dataset-path tasks \
  --exclude-task-id 'task_experimental_*' \
  --docker-image-strategy local-build
```

## Docker Image Strategies

LoopsBench defaults to `local-build`, the recommended strategy for benchmark runs. CLI options, YAML run configurations, and the Python `Harness` API use the same default. Three strategies are available:

| Strategy | Behavior | Typical use |
| --- | --- | --- |
| `local-build` (default, recommended) | Build images locally from each task's `docker-compose.yaml`. | Benchmark runs, task authoring, and validation. |
| `remote` | Pull prebuilt task images from a registry. | Optional use of published task images. |
| `local-existing` | Reuse images already present on the machine. | Debugging or offline reruns. |

When explicitly selected, `remote` defaults to the Docker Hub namespace `loopsbench` and tag `latest`. Task IDs are lowercased and underscores become hyphens: `task_tcp_course_stack` uses `loopsbench/loopsbench-task-tcp-course-stack:latest`. The namespace and tag can be overridden. Use this mode for tasks whose images have been published:

```bash
loopsbench run \
  --dataset-path tasks \
  --task-id task_tcp_course_stack \
  --agent codex \
  --model provider/model-name \
  --docker-image-strategy remote \
  --docker-image-namespace loopsbench \
  --docker-image-tag latest
```

## YAML Run Config

Most run options can be captured in a YAML file:

```yaml
dataset_path: tasks
output_path: runs
task_ids:
  - task_tcp_course_stack
exclude_task_ids: []
agent: codex
model: provider/model-name
docker_image_strategy: local-build
n_concurrent: 2
n_attempts: 1
cleanup: true
log_level: info
global_timeout_multiplier: 1.0
```

Run it with:

```bash
loopsbench run --config run.yaml
```

CLI flags override YAML values, so this is valid:

```bash
loopsbench run --config run.yaml --task-id task_tcp_course_stack --n-attempts 3
```

## Custom Agents

Use a built-in agent with `--agent`, or load a custom `BaseAgent` subclass with `--agent-import-path`:

```bash
loopsbench run \
  --dataset-path tasks \
  --agent-import-path my_package.my_agent:MyAgent \
  --docker-image-strategy local-build
```

Pass extra keyword arguments with repeatable `--agent-kwarg` entries:

```bash
loopsbench run \
  --dataset-path tasks \
  --agent codex \
  --agent-kwarg temperature=0 \
  --agent-kwarg max_turns=80 \
  --docker-image-strategy local-build
```

## Output Layout

A run writes metadata, per-task trial directories, pane logs, command logs, parsed results, and aggregate `results.json` files under the configured output path. The run status commands read those artifacts directly.
