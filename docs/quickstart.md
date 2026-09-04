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
  --model GPT-5.4 \
  --agent-kwarg 'command=["loopx","benchmark","agent-phase","--execute","--solver-command-json","[\"solver\",\"--mode\",\"task\"]"]' \
  --agent-kwarg arm_id=loopx \
  --agent-kwarg expected_provider=trae \
  --agent-kwarg provider_authority=trae-adapter \
  --agent-kwarg integrity_policy_sha256=<normalized-policy-sha256> \
  --docker-image-strategy local-build
```

The external command receives `LOOPSBENCH_EXTERNAL_AGENT_REQUEST`,
`LOOPSBENCH_EXTERNAL_AGENT_RESULT`, `LOOPSBENCH_EXTERNAL_AGENT_WORKSPACE`, and
`LOOPSBENCH_EXTERNAL_AGENT_TIMEOUT_SECONDS`. It must write the
`external_agent_result_v2` result to the supplied result path. The strict
`external_agent_request_v2` carries a public-safe
`benchmark_launch_admission_receipt_v0`; the result receipt must echo its
`launch_binding_digest`. Its provider-neutral `external_agent_phase_receipt_v2`
contains exactly the solver terminal classification, the three false raw-data
recording flags, command argument count, duration, instruction digest and
character count, and launch-binding digest. It carries no containment-success
claim. The command argument count describes the nested formal solver argv;
LoopsBench binds it to the admitted request's committed solver argv count and
preserves that inner count even when it differs from the outer bridge argv
count. The separately recorded outer command metadata remains only a redacted
argument count and digest. LoopsBench replaces duration with its own wall-clock
observation before publishing the compact receipt. The expected model comes
from the Harness-owned
top-level `--model`; expected provider, route authority, and integrity-policy
values are explicit preseed inputs, while run/case and effective
container/image bindings remain runner-owned. The controller computes
`integrity_policy_sha256` from its
normalized effective `benchmark_integrity_policy_v0`; LoopsBench validates and
binds that digest without duplicating policy normalization. This interface
does not give the command verifier, score, submission, or task-provisioning
authority.

`external-command` treats the task container as the runner-owned containment
boundary. Before provisioning it removes credential/controller variables from
the task-owned Compose environment and rejects Compose credential, secret,
config, and environment-file injection channels. Before solver launch it also
checks the effective container's namespace, network, capability, device,
security-option, mount, and environment boundaries. Its client has no mounts:
host log binds, `/shared`, and the hidden workspace mirror are all removed. The
tester separately retains `/shared` and its read-only task assets.
Runner-owned admission evidence and the compact result at
`agent-logs/trajectories/result.public.json` are written directly under the host
trial logging directory and are never mounted into the client. Provider profiles
and controller state are not valid task-container mounts. The configured command
is replaced in `run_metadata.json` before its first write by a SHA-256 digest,
argument count, and `command_recorded=false`; the private argv remains only in
runner memory. After normal completion or timeout, LoopsBench does not retain the
solver pane output. It pauses and verifies
the admitted container generation, and retains bounded raw-result and exact
`/workspace` archives in runner memory. It then kills and removes that client
and proves the captured container ID is absent before parsing the result or
preparing the verifier. Only then does it write the LoopsBench-owned
`agent-logs/trajectories/containment.public.json` proof. Its
`containment_absence_evidence_sha256` is SHA-256 of canonical JSON containing the
private admitted container ID, configured container name, containment receipt
reference, and `container_absent=true`; none of those private values are stored
in the public proof. This proof is bridge input, not the final LoopX trajectory
lineage receipt. Each trial invokes the external solver exactly once:
`outer_loop_count` must be `1` and
`rate_limit_retries_per_round` must be `0`. The run-level `--n-attempts` option
remains a separate, predeclared sampling dimension. The background regression
harness is disabled for this agent. Only after client-removal proof does the
runner parse and persist the compact public receipt, clear the tester workspace,
revalidate the admitted tester generation and its exact mount set, and transfer
the validated workspace archive byte-for-byte into it. The runner revalidates the
tester again and binds `TestRunner` to its immutable container ID before tests.
Archive capture and extraction enforce compressed-byte, expanded-byte, member
count, per-member, and result-JSON limits and reject sparse members. The fixed
in-container launcher keeps solver argv out of the host command log. Direct argv
is the supported provider-neutral seam: recognized POSIX shell interpreters may
execute a script file, but inline shell programs through `-c` or combined short
options are rejected, including when a dispatcher wraps the interpreter.
Likewise, GNU `env` split-string forms (`-S`, attached short-option forms such
as `-iS`, `--split-string`, and `--split-string=...`) are rejected fail-closed
instead of being reparsed by LoopsBench. Public route labels such as
`expected_provider` and `expected_model` must stay explicit public-safe labels;
credential-shaped or opaque values are rejected. Runner-owned public identity
fields such as run, case, arm, benchmark, and provider authority may be stored
as compact public-safe tokens or as typed `public:<kind>:sha256:<digest>`
digests when the supplied value is too opaque to persist verbatim. This direct
argv contract is not a general secret detector for arbitrary values embedded in
otherwise ordinary command arguments. If admission, snapshot, destruction, or
tester transfer cannot be verified, the trial records `agent_safety_error` and
the verifier does not start.

## Review Results

By default, run artifacts are written under `runs/<run-id>/`. Use:

```bash
loopsbench runs list --runs-dir runs
loopsbench runs status --run-id <run-id> --runs-dir runs
loopsbench runs summarize --run-id <run-id> --runs-dir runs
```
