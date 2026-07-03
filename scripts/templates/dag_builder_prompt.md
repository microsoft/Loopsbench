# Paper DAG → LHB Task Builder

You are building a Long-Horizon-Bench task from a paper DAG. Follow each step precisely. Update status.json after each step completes.

## Input

- **DAG Directory**: {{ dag_dir }}
- **Output Task Directory**: {{ output_dir }}/task_{{ dag_name }}
- **DAG Name**: {{ dag_name }}
- **Status File**: {{ dag_dir }}/status.json
- **Log File**: {{ dag_dir }}/build.log

## Resume State

{% if resume_from %}
Previous progress detected. Resume from step: **{{ resume_from }}**
Steps already completed: {{ steps_completed | join(", ") }}
{% else %}
Fresh start — no prior progress.
{% endif %}

## Status Update Protocol

After completing EACH step, update the status file by running:
```bash
python3 -c "
import json, datetime
s = json.load(open('{{ dag_dir }}/status.json'))
s['current_step'] = '<next_step_name>'
s['steps_completed'].append('<completed_step_name>')
s['updated_at'] = datetime.datetime.now().isoformat()
json.dump(s, open('{{ dag_dir }}/status.json', 'w'), indent=2)
"
```

Also append to the log file:
```bash
echo "[$(date -Iseconds)] STEP COMPLETED: <step_name>" >> {{ dag_dir }}/build.log
```

---

## Step 1: Read DAG Metadata (`read_dag`)

1. Read `{{ dag_dir }}/dag.json`
2. Parse nodes (papers) and edges (dependencies)
3. Compute topological sort of papers
4. Validate the graph is a DAG (no cycles)
5. Record the topological order — all subsequent steps use this order

The dag.json format:
```json
{
  "dag_name": "...",
  "description": "...",
  "nodes": [{"id": "paper_slug", "label": "Paper Title", "paper_path": "relative/path", "code_path": "relative/path", "layer": 0}],
  "edges": [{"from": "paper_a", "to": "paper_b", "type": "module_dependency", "label": "..."}]
}
```

---

## Step 2: Read Papers (`read_papers`)

For each paper node (in topological order):
1. Read the paper document at `{{ dag_dir }}/<paper_path>`
   - PDF: use the Read tool (handles PDFs natively)
   - txt/md/LaTeX: read directly
2. Extract and record for each paper:
   - **Key mechanisms/algorithms**: What the paper proposes (detailed enough to reimplement)
   - **Experiments**: What experiments the paper runs, metrics measured, numerical results reported
   - **Datasets used**: Names and sources of datasets for experiments
   - **Framework dependencies**: Existing systems/libraries the paper builds on
   - **Implementation language**: Programming language of the code

---

## Step 3: Read Code Repositories (`read_code`)

For each paper (in topological order):
1. Read the code at `{{ dag_dir }}/<code_path>/`
2. Understand:
   - Directory structure and file organization
   - Build system (Makefile, CMake, pip, npm, etc.)
   - Which files implement the paper's core contributions
   - Which files are framework/boilerplate (not paper-specific)
   - External dependencies (package.json, requirements.txt, etc.)
3. Identify the separation between "base framework" and "paper contribution"

---

## Step 4: Identify and Download Base Dependencies (`download_base`)

From your analysis of papers and code:
1. Identify external frameworks needed (e.g., ns3, SUMO, specific libraries)
2. Identify datasets needed for experiments (public datasets)
3. Create the output task directory: `mkdir -p {{ output_dir }}/task_{{ dag_name }}/base`
4. Download frameworks and datasets into `{{ output_dir }}/task_{{ dag_name }}/base/`:
   ```bash
   wget -P {{ output_dir }}/task_{{ dag_name }}/base/ <dataset_url>
   git clone <framework_repo> {{ output_dir }}/task_{{ dag_name }}/base/<framework_name>
   ```
5. Record what was downloaded in the log

---

## Step 5: Construct base/ (`construct_base`)

Assemble `{{ output_dir }}/task_{{ dag_name }}/base/` containing:
1. Framework code (from Step 4)
2. Datasets for experiments (from Step 4)
3. Build configuration files (Makefile, CMakeLists, package.json, etc.)
4. Skeleton/boilerplate files the framework expects

**Critical rules:**
- base/ must NOT contain any paper-specific implementation code
- base/ IS the starting point — it contains only what's needed to begin
- Include build configs so the agent can run `make` or equivalent immediately
- If the papers share a common framework, that framework goes in base/

---

## Step 6: Generate gold_patches/ (`generate_patches`)

Create `{{ output_dir }}/task_{{ dag_name }}/gold_patches/`

Work in a temporary directory:
1. `cp -r {{ output_dir }}/task_{{ dag_name }}/base /tmp/gold_work_{{ dag_name }}`
2. `cd /tmp/gold_work_{{ dag_name }} && git init && git add -A && git commit -m "base"`
3. For each paper in topological order:
   a. Apply that paper's implementation from its code repo to the working area
   b. `git add -A`
   c. `git diff --cached > {{ output_dir }}/task_{{ dag_name }}/gold_patches/<paper_slug>.diff`
   d. `git commit -m "impl: <paper_slug>"`
4. Clean up: `rm -rf /tmp/gold_work_{{ dag_name }}`

**Key**: Each paper's diff is INCREMENTAL — only the changes that paper adds on top of all previous papers.

---

## Step 7: Extract requirements/ (`extract_requirements`)

Create `{{ output_dir }}/task_{{ dag_name }}/requirements/`

For each paper (in topological order), create `requirements/<paper_slug>.yaml`:

```yaml
id: "<paper_slug>"
title: "<Paper Title>"
category: <domain>
requirement: |-
  ## Context
  <1-2 sentences: what overall project does, what's been done so far>

  ## Module: <Paper Title>
  <Detailed description of key mechanism/algorithm>
  <Enough detail for a skilled developer to reimplement>
  <Include pseudocode/algorithm steps if applicable>

  ## Experiments
  ### Experiment 1: <name>
  - **What to measure**: <metric>
  - **Input/Setup**: <data/config>
  - **Expected output format**: EXPERIMENT_NAME: VALUE

  ### Experiment 2: <name>
  ...

  ## Instructions
  - Implement the mechanism described above
  - Create a `run.sh` script that:
    - Runs ALL experiments listed above
    - Outputs results as: `EXPERIMENT_NAME: VALUE` (one per line)
    - Each result is a single numeric value
  - Ensure integration with previously implemented modules

  ## Files to modify
  <List files>

  ## Acceptance signals
  <Key function signatures or output patterns>
```

---

## Step 8: Generate tests/ (`generate_tests`)

Create `{{ output_dir }}/task_{{ dag_name }}/tests/`

### Step 8a: Get reference results

For each paper (use the gold working directory from Step 6):
1. With patches applied up to this paper, run the experiments
2. Record gold_results (actual numbers from running gold code)
3. Compare with paper_results (numbers stated in the paper)
4. Compute tolerance per experiment: `max(abs(gold - paper), abs(paper) * 0.05)`

### Step 8b: Write test files

For each paper, create `tests/<paper_slug>/test_outputs.py`:

```python
"""Tests for: <paper_title>"""
import os
import subprocess
import pytest

WORKSPACE = "/workspace"
EXPECTED_EXPERIMENTS = {
    "<EXP_1_NAME>": {"value": <float>, "tolerance": <float>},
    "<EXP_2_NAME>": {"value": <float>, "tolerance": <float>},
}


@pytest.fixture(scope="module")
def run_experiments():
    """Execute run.sh and capture output."""
    run_sh = os.path.join(WORKSPACE, "run.sh")
    assert os.path.isfile(run_sh), "run.sh not found"
    assert os.access(run_sh, os.X_OK), "run.sh not executable"
    result = subprocess.run(
        ["bash", run_sh], cwd=WORKSPACE,
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, f"run.sh failed:\n{result.stderr}"
    return result.stdout


def parse_results(stdout):
    """Parse KEY: VALUE lines."""
    results = {}
    for line in stdout.strip().split("\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            try:
                results[key.strip()] = float(val.strip())
            except ValueError:
                continue
    return results


def test_experiment_count(run_experiments):
    """All expected experiments present."""
    results = parse_results(run_experiments)
    missing = set(EXPECTED_EXPERIMENTS.keys()) - set(results.keys())
    assert not missing, f"Missing: {missing}"


@pytest.mark.parametrize("exp_name,expected", EXPECTED_EXPERIMENTS.items())
def test_experiment_value(run_experiments, exp_name, expected):
    """Each result within tolerance."""
    results = parse_results(run_experiments)
    assert exp_name in results, f"'{exp_name}' not in output"
    actual = results[exp_name]
    assert abs(actual - expected["value"]) <= expected["tolerance"], (
        f"'{exp_name}': got {actual}, expected {expected['value']} +/- {expected['tolerance']}"
    )
```

---

## Step 9: Generate Metadata (`generate_metadata`)

### slug_diff_map.json
```json
{
  "<paper_a>.yaml": "gold_patches/<paper_a>.diff",
  "<paper_b>.yaml": "gold_patches/<paper_b>.diff"
}
```

### module_dag.yaml
```yaml
project: "<dag description>"
description: >
  <Multi-sentence description of the paper chain>

nodes:
  - id: <paper_slug>
    label: "<Paper Title>"
    path: "<main file>"
    description: >
      <Paper's contribution>
    files_count: <N>
    loc: <approx lines>
    impl_order: <1-based>

edges:
  - from: <paper_a>
    to: <paper_b>
    label: "<dependency>"
```

### unit_dag.json
```json
{
  "repo_id": "{{ dag_name }}",
  "total_units": <N>,
  "num_layers": <N>,
  "nodes": [{"id": "<slug>", "layer": <N>, "has_tests": true}],
  "edges": [{"from": "<a>", "to": "<b>", "type": "module_dependency", "label": "..."}]
}
```

### task.yaml
```yaml
instruction: |-
  <Project description, working dir, modules to implement, test-coupled signatures>
author_name: LHB Dataset Author
author_email: dataset@long-horizon-bench.example
difficulty: hard
category: <domain>
tags: [<tags>]
parser_name: pytest
max_agent_timeout_sec: 7200
max_test_timeout_sec: 300.0
run_tests_in_same_shell: false
expert_time_estimate_min: <paper_count * 30>
junior_time_estimate_min: <paper_count * 90>
```

---

## Step 10: Generate Docker Assets (`generate_docker`)

### Dockerfile
```dockerfile
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y \
    tmux <lang-packages> patch python3-pytest curl git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
COPY base/ /workspace/
COPY requirements/ /workspace/requirements/
```

### docker-compose.yaml
```yaml
services:
  client:
    build: .
    volumes:
      - shared_data:/shared
    environment:
      - TEST_DIR=/tests
    stdin_open: true
    tty: true
  tester:
    build: .
    volumes:
      - shared_data:/shared
      - ./tests:/tests:ro
      - ./gold_patches:/workspace/gold_patches:ro
    environment:
      - TEST_DIR=/tests
volumes:
  shared_data:
```

---

## Step 11: Generate solution.sh and run-tests.sh (`generate_scripts`)

### solution.sh (FTP-style: patch → test → next)
```bash
#!/bin/bash
set -e
PATCH_ROOT="${GOLD_PATCHES_DIR:-/workspace/gold_patches}"
TEST_DIR="${TEST_DIR:-/tests}"
cd /workspace

curl -LsSf https://astral.sh/uv/0.7.13/install.sh | sh
source $HOME/.local/bin/env
uv venv .lhb-testing
source .lhb-testing/bin/activate
uv pip install pytest==8.4.1

papers=(<PAPER_SLUGS_TOPO_ORDER>)

for paper in "${papers[@]}"; do
  echo "[oracle] Applying: ${paper}.diff"
  if [ -s "${PATCH_ROOT}/${paper}.diff" ]; then
    patch -p1 < "${PATCH_ROOT}/${paper}.diff"
  fi
  echo "[oracle] Testing: ${paper}"
  uv run pytest "${TEST_DIR}/${paper}/test_outputs.py" -rA
  echo "[oracle] PASSED: ${paper}"
done
echo "[oracle] All patches applied and tested successfully"
```

### run-tests.sh (FTP-style)
```bash
#!/bin/bash
set -e
apt-get update && apt-get install -y curl
curl -LsSf https://astral.sh/uv/0.7.13/install.sh | sh
source $HOME/.local/bin/env

if [ "$PWD" = "/" ]; then
    echo "Error: No working directory set."
    exit 1
fi

uv venv .lhb-testing
source .lhb-testing/bin/activate
uv pip install pytest==8.4.1
TEST_DIR="${TEST_DIR:-/tests}"

papers=(<PAPER_SLUGS_TOPO_ORDER>)
for paper in "${papers[@]}"; do
  echo "[test] Testing: ${paper}"
  uv run pytest "${TEST_DIR}/${paper}/test_outputs.py" -rA
done
```

Both must be `chmod +x`.

---

## Step 12: FTP Validation (`validate`)

1. Run: `python3 {{ output_dir }}/../scripts/validate_per_pr.py --task-dir {{ output_dir }}/task_{{ dag_name }} --strict-fail-to-pass`
2. If fails:
   - Read error → diagnose (patch/test/tolerance issue)
   - Fix → retry (up to 5 times)
3. On success: update status.json to `"completed"`
4. On exhausted retries: update to `"failed"` with error description

---

## Final Verification

Before marking complete:
```bash
ls {{ output_dir }}/task_{{ dag_name }}/{base,gold_patches,requirements,tests,Dockerfile,docker-compose.yaml,solution.sh,run-tests.sh,slug_diff_map.json,module_dag.yaml,unit_dag.json,task.yaml}
```
All must exist.
