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

Environment preparation has network access. During solving, the agent's task container can reach the model endpoint hosts from the run configuration. By default, tests run without external network access, so their dependencies need to be installed in the task image. Tasks opting into the controlled dependency service below can install dependencies through that service during solving and testing.

To allow additional hosts during solving, add `--allow-agent-host HOST` for each host or list them under `network.extra_allowed_hosts` in the run YAML.

These settings apply to task containers. Host-side tools and search features provided by a model service need to be configured separately.

### Controlled dependency service

The first supported package protocols are **Python/PyPI** and **Go modules with checksum verification**. Other ecosystems are rejected explicitly. Rust registries, Maven/Gradle, npm, Git dependencies, operating-system packages and toolchain downloads are not enabled by this option; prepare their required tools/dependencies in the image until corresponding adapters are available.

Enable access per task. Existing task directories need no edits when using run-file overrides:

```yaml
# run.yaml
dataset_path: tasks
task_ids: [my_go_task, my_python_task]
dependency_service:
  provider: docker
task_dependency_overrides:
  my_go_task:
    mode: managed
    ecosystems: [go]
    blocked_packages:
      go: [github.com/owner/target, github.com/old-owner/target]
    go_modules: [.] # List module directories for a multi-module project.
  my_python_task:
    mode: managed
    ecosystems: [python]
    blocked_packages:
      python: [target-project, previous-published-name]
    python:
      project: . # Editable install of the submitted project; null to skip.
      requirements_files: [] # Optional submitted requirements files.
      test_requirements: [pytest==8.3.4] # Trusted test tools, not solver inputs.
    prepare_timeout_sec: 600
```

The same policy may be placed under `dependency_access` in `task.yaml`. A run-file override replaces that task's policy. `mode: disabled` retains the ordinary network plan. Include the target's actual published package identities and known aliases in `blocked_packages`; these trusted values never come from a candidate-edited manifest. Python names are normalized, and a blocked Go module also blocks its submodules and `/v2`, `/v3`, etc. Toolchain module downloads are blocked.

The harness creates one package gateway per trial and configures normal `pip` and `go` commands to use it. During solving, the client can reach its model hosts and the gateway; the tester can reach only the gateway. Package registry domains are **not** added to the container allowlist. Setting `network.agent` or `network.verifier` explicitly to `no-network` conflicts with `mode: managed` and fails configuration. Public solver/tester access also conflicts with managed mode. Agent installation retains the original package-manager settings and environment network policy; the host switches read-only pip/Go configuration files for solving and restores them afterwards. Built-in/custom agents and custom model URLs use their existing interfaces.

Before each direct or regression test, the harness copies the snapshot, clears the tester's managed dependency state, and prepares it under the restricted tester policy:

- Python: create a fresh virtual environment without system site packages, install the submitted project and configured requirements, then run the task tests in that environment. The image needs Python with `venv`/`ensurepip`. Install build requirements through the gateway; retain required native compilers and OS libraries in the image. The local project is installed before test tools so a dependency on the target project can resolve locally.
- Go: clear the tester module/build caches, download the submitted modules, and record the selected module graph. Use the gateway for module files and signed `sum.golang.org` records, with no direct VCS or automatic toolchain fallback. The image needs Go, Python 3 for the trusted launcher, and the required compiler/toolchain.

Both need Bash and `timeout`; install snapshot-copy tools such as `rsync` in the image. Solver installations and tester installations remain separate. Only the gateway's downloaded bytes are shared. Preparation logs are kept separate from scored test output. Build/install hooks run as untrusted code in the restricted tester, never in the unrestricted agent preparation phase.

The gateway exposes package-specific metadata and download routes, not arbitrary URLs, search, uploads, or a general HTTP proxy. It checks policy before every cache lookup and checks upstream redirects against operator-configured artifact hosts. This controls package identities and access channels; legitimate dependency distributions can still contain source code.

Each trial's host-only `dependencies/` directory (beside `logs/`, which legacy tasks may mount) contains the effective policy, service image ID, preparation logs with selected versions, and gateway data. It is never mounted as a directory into client/tester; only specific runtime configuration files are mounted read-only. `data/resolution.json` maps requested upstream resources to SHA256-addressed blobs; metadata/version lists and 404 results are frozen on first use for that trial. `data/requests.jsonl` records accepted/rejected requests. Gateway health checks do not certify availability of every upstream. A gateway/upstream/cache error during a test is reported as `dependency_infrastructure_error`, never counted as a passing test or a regression transition, and never triggers unrestricted fallback. An unavailable package version (404), blocked dependency (403), or invalid manifest is a candidate failure instead.

Replay the captured dependency universe without contacting upstreams:

```yaml
dependency_service:
  provider: docker
  replay_from: /absolute/path/to/previous/trial/dependencies/data
```

The harness copies the data for the new trial and checks its current package policy before serving any cached resource. A replay cache miss is an infrastructure error. Reproduce with the same task image, toolchains, task policy and manifests; a download cache alone does not pin those inputs. Successful normal runs preserve their data when containers are removed.

Operators can point the gateway at registry mirrors using `python_metadata_url` (PyPI JSON API), `python_artifact_hosts`, `go_proxy_url`, `go_artifact_hosts`, and `go_sumdb_url`. Upstreams use HTTPS by default, exact redirect hostnames, bounded request time/size, and no inherited generic proxy. URL credentials are rejected. `allow_insecure_upstreams: true` is intended for local registry integration tests. Private registry authentication requires a separately configured operator mirror; credentials are not passed to candidates.

For an existing service, set `provider: external` and `endpoint: https://dedicated-gateway.example.org`. It must implement the LoopsBench gateway protocol (the included `loopsbench/dependencies/server.py`), report protocol version 1 and the exact task policy digest at `/status`, and serve **only that policy at this origin**, without administrative or general proxy routes. A general package mirror is not a suitable external endpoint. The harness checks its policy and error counters before and after tests; the operator retains its audit/cache data. Configure frozen replay at that external service.

These options affect LoopsBench execution. They do not change a dataset's historical acceptance or an external construction pipeline's independent validation policy.

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
