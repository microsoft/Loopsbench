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

Restricted phases use a static DNS snapshot: the trusted sidecar resolves each exact authorized hostname when the policy is applied, and serves only A/AAAA records for those names. Task DNS packets are never forwarded upstream. TXT/ANY queries, arbitrary subdomains and unlisted names are rejected over both UDP and TCP, including attempts to reach Docker's embedded resolver at its translated ports. Wildcard hostnames are rejected; IPs and CIDRs remain valid network policy entries but do not authorize DNS names. Resolution failures abort the phase switch with networking restricted. Addresses remain frozen until the next policy application; apply a new policy to refresh an endpoint whose IPs changed. Public preparation retains ordinary DNS access.

Docker shares `/etc/hosts` and `/etc/resolv.conf` with containers joining a network namespace. Before starting the task, the sidecar privately captures the trusted bootstrap hosts/resolvers and configures its own Go resolver to skip the shared hosts file. DNS snapshot refreshes use only those captured nameservers and trusted host overrides, even if the candidate later rewrites the shared files.

These settings apply to task containers and preserve the existing built-in/custom agent interfaces. Once solving starts, the client keeps the agent policy through round boundaries, rate-limit backoff, failures, test execution and artifact collection until teardown. It never returns to the public preparation policy. The tester keeps its independent verifier policy. Host-side tools and search features provided by a model service remain outside this network boundary.

Agent setup has two optional hooks. `install(session, logging_dir)` runs once per client container and agent class/version, before the first solve. Its successful return value is retained on the host and exposed as `agent.installation` to later agent instances in that trial. `prepare(session, logging_dir)` still runs on every attempt, including new outer-loop sessions, to configure credentials, shell environment and per-round files. Built-in agents reuse their installed tools; no task files or new abstract plugin methods are required. Legacy custom agents keep their existing `prepare` calls, but repeated downloads after the first solve are now restricted. Move one-time downloads into `install` and leave local session initialization in `prepare`. A later installation failure never restores public access; use a fresh trial/container if installation must be repeated with preparation networking.

### Controlled dependency service

The gateway supports **PyPI, Go modules, npm, Cargo sparse registries, Maven repositories (including Gradle plugins), NuGet v3 flat containers, and configured apt repositories**. Package access and snapshot installation are separate: enabling a protocol does not require its package manager in every task image. Ordinary dependency packages and versions need no individual approval. Target package identities and aliases are blocked by trusted task policy.

Published tasks should declare their dependency policy in `task.yaml` so a normal CLI run needs no separate dependency configuration:

```yaml
# task.yaml (alongside instruction, timeouts, etc.)
dependency_access:
  mode: managed
  ecosystems: [maven]
  prepare: [] # The task's trusted test runner performs the build.
  blocked_packages:
    maven: [com.example:target-project]
dependency_service:
  maven_repository_urls:
    - https://repo.maven.apache.org/maven2
    - https://repo.jenkins-ci.org/public
  maven_artifact_hosts:
    - repo.maven.apache.org
    - repo.jenkins-ci.org
    - jfrog-prod-use1-shared-virginia-main.s3.amazonaws.com
```

`dependency_access` enables managed access; `dependency_service` alone does not. Omit `dependency_service` when the built-in registries suffice. Service settings resolve independently for each task: explicit run-file fields override task fields, which override built-in defaults. Lists and mappings are replaced as whole fields. For example, a run setting only `provider: docker` or a download concurrency limit preserves task-specific Maven repositories; an explicit `maven_repository_urls` replaces that list. Keep machine-specific cache/replay paths and external gateway endpoints in run configuration.

For experiments or older tasks, run-file overrides remain available:

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

A run-file `task_dependency_overrides` entry replaces that task's entire `dependency_access` policy. `mode: disabled` retains the ordinary network plan. Include the target's actual published identities and known aliases, including publications in other ecosystems, in `blocked_packages`; these trusted values never come from candidate-edited manifests. Python names are normalized; Cargo treats hyphens/underscores as aliases; NuGet and npm identities are case-normalized. Maven identities use `groupId:artifactId`. A blocked Go module also blocks submodules and `/v2`, `/v3`, etc. Go toolchain module downloads are blocked. apt checks binary and source package names before delivering `.deb` files.

To reject unlisted forks and other packages, additionally provide `allowed_packages` with an exact identity list for **every** enabled ecosystem (an empty list denies that ecosystem's packages). The list comes from trusted task authoring/review, including the required transitive dependencies, and never expands from a solver's manifest or from live registry metadata. Versions are unrestricted within approved identities, so dependency upgrades remain possible. New package identities, including Go major-version module paths, need a trusted policy update. Target denials take precedence, and authorization is checked before serving cached bytes. Omitting `allowed_packages` retains the original blacklist-only behavior and does not block unknown forks. Approved distributions can still contain arbitrary source; this is identity authorization, not a content-provenance guarantee.

```yaml
task_dependency_overrides:
  example_task:
    ecosystems: [go]
    blocked_packages:
      go: [github.com/example/target]
    allowed_packages:
      go: [github.com/google/uuid]
```

Omit `ecosystems` to enable all supported protocols. `prepare` defaults to detecting root manifests; set it explicitly to select installation plugins, or `[]` for protocol access without automatic installation. Use `projects` to name subdirectories in mixed-language/monorepo tasks (Python retains its `python` configuration; Go also accepts `go_modules`):

```yaml
task_dependency_overrides:
  my_rust_with_web_task:
    blocked_packages:
      cargo: [target-crate, old-crate-name]
      npm: ['@owner/target-web']
    prepare: [cargo, npm]
    projects:
      cargo: ['.']
      npm: [web]
```

The harness creates one package gateway per trial and configures ordinary package-manager commands to use it. During solving, the client can reach its model hosts and the gateway; the tester can reach only the gateway. Package registry domains are **not** added to the container allowlist. Explicit `network.agent` or `network.verifier` `no-network` and public access both conflict with managed mode. Initial agent installation retains the original environment/network settings: the host switches read-only pip/Go configuration files and phase-aware wrappers for `npm`, `npx`, `cargo`, `mvn`, `gradle`, `dotnet`, `apt` and `apt-get`. The wrappers preserve the image PATH and delegate unchanged before the first solve. Once solving starts, gateway configuration remains active across rounds and cleanup. Built-in/custom agents and custom model URLs keep their existing interfaces. Absolute-path invocations and project wrappers may need explicit repository configuration; the firewall still enforces the same boundary.

Before each direct or regression test, the harness copies the snapshot, clears the tester's managed dependency state, and prepares it under the restricted tester policy:

- Python: create a fresh virtual environment without system site packages, install the submitted project and configured requirements, then run the task tests in that environment. The image needs Python with `venv`/`ensurepip`. Install build requirements through the gateway; retain required native compilers and OS libraries in the image. The local project is installed before test tools so a dependency on the target project can resolve locally.
- Go: clear the tester module/build caches, download the submitted modules, and record the selected module graph. Use the gateway for module files and signed `sum.golang.org` records, with no direct VCS or automatic toolchain fallback. The image needs Go, Python 3 for the trusted launcher, and the required compiler/toolchain.

- npm: remove project `node_modules` and the managed npm cache, then use `npm ci` when a lockfile exists or `npm install` otherwise. The default `npm.install_mode: locked` rejects a manifest/lockfile mismatch. Trusted task/run policies can explicitly select `npm.install_mode: update` for legacy upgrade tasks: run `npm install` against the submitted manifest and seed lockfile, updating only the disposable tester copy. The candidate snapshot is unchanged; the selected dependency tree is recorded. `npm.legacy_peer_deps: true` opts into npm's legacy peer handling. Artifact integrity checks and gateway restrictions apply in both modes; audit/funding calls are disabled and npm uses at most 8 sockets by default. Yarn/pnpm-specific lockfile installation is not an automatic plugin.
- Cargo: use a fresh Cargo home and target directory, replace crates.io with the gateway sparse registry, then run `cargo fetch` and `cargo metadata`. Retain the image's rustup toolchains and override inherited offline mode for managed access.
- Maven: clear managed local repository and module `target` directories; run `dependency:resolve` for test scope. For multi-module reactors, build/install submitted sibling modules locally with test compilation/execution skipped, so resolution does not fetch their published counterparts. Mirror repositories through the gateway; lifecycle plugins may download additional artifacts during testing.
- Gradle: clear managed Gradle home and module build state; run `gradle dependencies`. An init script configures dependency and plugin repositories. Use an installed Gradle 7+ compatible with the task; this does not automatically authorize wrapper distribution downloads.
- NuGet: use fresh package/HTTP caches, clear project `bin`/`obj`, then run `dotnet restore` using only the gateway source. Disable NuGet audit network requests; preserve normal package integrity/signature verification.

With `npm.legacy_peer_deps: true`, npm can install successfully while `npm ls --all` reports `ELSPROBLEMS` for ignored peers. The harness records that complete diagnostic tree and explicitly logs the compatibility decision, then requires `npm ls --depth=0` to validate direct requirements. A successful installation is still mandatory. This opt-in mode does not require a clean transitive `npm ls` inventory; other inventory errors and direct dependency failures remain fatal. Default strict mode requires the complete inventory to pass.

Images need the selected package managers/toolchains, Python 3 for the launcher, Bash and `timeout`. `rsync` is optional (snapshot copying falls back to `cp`). Solver and tester installations remain separate; only gateway download bytes are shared. Preparation logs are separate from scored output. Build/install hooks run as untrusted code in the restricted tester, never in the unrestricted agent preparation phase. Manifests may change versions, but cannot authorize a new upstream host.

apt needs operator-configured repositories matching the image distribution and installed signing keyrings. Signed Release/Packages files are served unchanged; `.deb` downloads are checked against their indexed identities and SHA256. No `trusted=yes` or signature bypass is added:

```yaml
dependency_service:
  apt_repositories:
    debian:
      url: https://deb.debian.org/debian
      suites: [bookworm, bookworm-updates]
      components: [main]
      architectures: [amd64]
      signed_by: /usr/share/keyrings/debian-archive-keyring.gpg
```

When apt access has configured repositories, each test recreates the tester container's writable layer and anonymous volumes before restoring the snapshot. This avoids retaining `dpkg`/system-library changes from earlier evaluations. Snapshot preservation uses the image's `tar` within the container so tmpfs contents are included. Writable task volumes containing system directories are rejected in this mode; workspace/snapshot/shared/log mounts remain supported. apt is a download/install interface, not a manifest preparation plugin: keep fixed OS prerequisites in the image or declare installation in the task's existing trusted test script. Snapshot manifest detection does not invent OS package requirements.

The gateway exposes package-specific metadata and download routes, not arbitrary URLs, search, uploads, or a general HTTP proxy. It checks policy before every cache lookup and checks upstream redirects against operator-configured artifact hosts. This controls package identities and access channels; legitimate dependency distributions can still contain source code.

Each trial's host-only `dependencies/` directory (beside `logs/`, which legacy tasks may mount) contains the effective policy, service image ID, preparation logs with selected versions, and gateway data. It is never mounted as a directory into client/tester; only specific runtime configuration files are mounted read-only. `data/resolution.json` maps requested upstream resources to SHA256-addressed blobs; metadata/version lists and 404 results are frozen on first use for that trial. `data/requests.jsonl` records accepted/rejected requests. Gateway health checks do not certify availability of every upstream. A gateway/upstream/cache error during a test is reported as `dependency_infrastructure_error`, never counted as a passing test or a regression transition, and never triggers unrestricted fallback. An unavailable package version (404), blocked dependency (403), or invalid manifest is a candidate failure instead.

The gateway downloads independent resources concurrently (`dependency_service.max_concurrent_downloads`, default 4), while concurrent requests for the same resource and representation share one upstream fetch. `max_concurrent_requests` (default 4) also bounds the entire request lifetime, including cached metadata decoding, rewriting and delivery; limiting downloads alone does not bound this memory use. npm has a tighter `max_concurrent_npm_requests` limit (default 2). At most `max_pending_requests` (default 32) additional handlers can wait; excess connections wait in the TCP backlog rather than immediately receiving 503. Queue waits use `request_timeout_sec`. The Docker gateway has a separate loopback-only health/status listener on port 8081 so package bursts cannot starve its control checks. npm requests the abbreviated install metadata representation, bounded by `max_npm_metadata_bytes` (default 16 MiB) before JSON parsing. Cache keys include the Accept representation, so replaying a cache captured before this change may require recapturing npm metadata.

The transparent proxy allows up to 330 seconds for response headers, covering the gateway's configurable upstream timeout; the task's total evaluation timeout still applies. The egress image builds pinned GOST sources including the per-request HTTP Host/HTTP2 authority authorization fix; a newer image date alone does not establish that the fix is present. Broken response delivery and gateway connection failures in the trusted proxy logs also invalidate the evaluation. Proxy evidence is saved in `dependencies/transport-errors.json`, independently of candidate output.

The DNS snapshot closes ordinary external DNS queries in restricted task containers, and explicit package identity allowlists reject unlisted forks. Model endpoint access remains host-based: arbitrary API paths on an authorized host are reachable, and TLS request bodies are not inspected. Provider-hosted search and host-side tools/MCP are outside this boundary. This configuration provides ordinary phase-based networking plus controlled dependency downloads; it does not certify complete search isolation.

Replay the captured dependency universe without contacting upstreams:

```yaml
dependency_service:
  provider: docker
  replay_from: /absolute/path/to/previous/trial/dependencies/data
```

The harness copies the data for the new trial and checks its current package policy before serving any cached resource. A replay cache miss is an infrastructure error. Reproduce with the same task image, toolchains, task policy and manifests; a download cache alone does not pin those inputs. Successful normal runs preserve their data when containers are removed.

Operators can use Nexus, Artifactory or another protocol-compatible artifact server as upstream storage. The LoopsBench gateway remains in front to enforce task policy. Configure standard protocol endpoints:

| Protocol | Upstream setting | Additional redirect/artifact hosts |
| --- | --- | --- |
| PyPI | `python_simple_url` (PEP 503, SHA256 links), or `python_metadata_url` (PyPI JSON) | `python_artifact_hosts` |
| Go | `go_proxy_url`, `go_sumdb_url` | `go_artifact_hosts` |
| npm | `npm_registry_url` | `npm_artifact_hosts` |
| Cargo | `cargo_index_url`, `cargo_download_url` | `cargo_artifact_hosts` |
| Maven/Gradle | `maven_repository_urls` (ordered list) | `maven_artifact_hosts` |
| NuGet | `nuget_flat_url` (PackageBaseAddress) | `nuget_artifact_hosts` |
| apt | `apt_repositories` | Same-origin repository URLs |

Cargo download templates support `{crate}`, `{version}`, `{prefix}` and `{lowerprefix}`. Maven falls back to the next configured repository only on 404, never on outages. Upstreams require HTTPS, exact redirect hostnames, bounded time/size, and no inherited general proxy. URL credentials are rejected. `allow_insecure_upstreams: true` is for local integration tests. Private registry authentication requires an operator mirror; credentials are not passed to candidates.

Git dependencies, direct URLs and toolchain installers do **not** get arbitrary internet access. For a dependency without a supported registry protocol, the trusted task/run policy can name an exact immutable artifact, such as a source archive, Git bundle or toolchain tarball:

```yaml
task_dependency_overrides:
  my_task:
    blocked_packages:
      cargo: [target-crate]
    sources:
      native-sdk:
        url: https://downloads.example.org/sdk-1.2.tar.gz
        filename: sdk-1.2.tar.gz
        sha256: REPLACE_WITH_64_LOWERCASE_HEX_DIGITS
        redirect_hosts: []
```

The artifact is available at `http://loopsbench-dependencies:8080/client/source/native-sdk/sdk-1.2.tar.gz` (use the configured endpoint for an external gateway, and `/tester/` during tests). Hashes are checked on downloads and cache hits. Installation remains in the restricted task container. Normal `git clone`/arbitrary Git smart-HTTP endpoints are not provided; a trusted source archive or Git bundle must be used explicitly. Candidate edits never add these declarations. Additional ecosystems can add protocol and preparation plugins independently; pinned artifacts provide an escape hatch without making the service a general web proxy.

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
