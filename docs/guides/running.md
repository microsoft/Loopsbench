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

We recommend `local-build` for benchmark runs. It is the default and builds each task's image from the supplied Dockerfile.

| Strategy | Behavior | Typical use |
| --- | --- | --- |
| `local-build` (default) | Build images locally using each task's Compose configuration. | Benchmark runs and task development. |
| `remote` | Pull prebuilt task images from a registry. | Running tasks with published images. |
| `local-existing` | Reuse images already present on the machine. | Debugging or offline reruns. |

Use `remote` to pull a published task image from Docker Hub. The default namespace is `loopsbench` and the default tag is `latest`:

```bash
loopsbench run \
  --dataset-path tasks \
  --task-id task_tcp_course_stack \
  --agent codex \
  --model provider/model-name \
  --docker-image-strategy remote
```

For example, `task_tcp_course_stack` uses `loopsbench/loopsbench-task-tcp-course-stack:latest`. Pass `--docker-image-namespace` or `--docker-image-tag` to use another namespace or version.

## Network Access

Environment preparation has network access. During solving, the agent's task container can reach the model endpoint hosts from the run configuration. Tests run without external network access, so their dependencies need to be installed in the task image.

To allow additional hosts during solving, add `--allow-agent-host HOST` for each host or list them under `network.extra_allowed_hosts` in the run YAML.

These settings apply to task containers. Host-side tools and search features provided by a model service need to be configured separately.

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
