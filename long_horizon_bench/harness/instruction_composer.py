"""Shared instruction composition for benchmark agents."""

from __future__ import annotations

from long_horizon_bench.agents.agent_name import AgentName


_STRONG_AGENTS = {
    AgentName.CLAUDE_CODE,
    AgentName.QWEN_CODE,
}
_STRUCTURED_OPEN_SOURCE_AGENTS = {
    AgentName.SWE_AGENT,
    AgentName.OPENHANDS,
}
_LEGACY_VERBOSE_AGENTS = {
    AgentName.MINI_SWE_AGENT,
}

_BENCHMARK_INTRO = """

You are running as a long-horizon autonomous engineering agent at
`/workspace`. This task spans many files and requirements, and is expected
to take many iterations.

Read every requirement under `/workspace/requirements/` (and any other
explicit requirement list referenced by the task) and only finish once
every single one is addressed. Cherry-picking or partial fixes will lead
to failure.
""".rstrip()

_OPTIONAL_PLANNING_ARTIFACT = """

## Optional planning artifact

If you believe the task is complex enough to benefit from a plan, you may
write `/workspace/agent_plans/plan.json` before making changes. This is
optional. If you create it, it must be a JSON object with `nodes` and
`edges`. Each node must have string `id` equal to a requirement slug from
`/workspace/requirements/`, may include `label` and `layer`, and each edge
must use string `from` and `to` that reference those ids.
""".rstrip()

_BENCHMARK_FROM_GIT = """

## Git workflow (required)

`/workspace` is initialized as a git repo. After finishing the
implementation for ONE requirement:

  1. `cd /workspace`
  2. `git add -A`
  3. `git diff --cached > requirement_patches/<slug>.diff`
  4. `git commit -m "impl: <slug>"`

Where `<slug>` is the requirement filename without the `.yaml` extension.
The patch file MUST be non-empty for the requirement to count as complete.
Never write or `touch` an empty `.diff` file.

You may run `git log`, `git diff`, `git show` at any time to inspect what
prior work has done — only your own commits will be there; no reference
solution is accessible.

## Recommended workflow

1. Read the task carefully and survey `/workspace` so you understand the
   code you are changing.
2. As a development agent, when implementing a requirement you may write
   tests under `/workspace/agent_tests/` to safeguard correctness and
   future stability — write them if you think they help, otherwise skip.
3. Keep iterating until every requirement is implemented. Do not stop at a
   partial fix, a first passing check, or one successful edit.
4. Do not narrow the scope on your own. Missing a requirement is a task
   failure even if the changed tests pass.
5. You may revise the same files and rerun focused checks as many times as
   needed before finishing.
6. Your final summary must report requirement coverage counts, including at
   least the total number of requirements reviewed and how many were
   implemented and blocked.
""".rstrip()

_PROFILE_ADDENDA = {
    "strong": """

Agent profile:
- Focus on completing the task directly.
- Do not spend tokens on basic shell tutorials or extended environment narration.
- Do not silently narrow the task scope, claim success early, or stop after partial requirement coverage.
- Use concise progress updates, keep modifying the workspace when needed, and validate the parts you change.
""".rstrip(),
    "structured_open_source": """

Agent profile:
- Work step by step and keep actions grounded in the repository state.
- Prefer short, concrete progress updates and targeted validation.
- Avoid unnecessary tutorial-style shell explanation.
""".rstrip(),
    "legacy_verbose": """

Agent profile:
- You may use a slightly more explicit step-by-step style when it helps execution.
- Keep progress updates concrete and repository-specific.
- Prefer direct validation of changed behavior before finishing.
""".rstrip(),
}


def _resolve_profile(agent_name: AgentName | None) -> str | None:
    if agent_name in _STRONG_AGENTS:
        return "strong"
    if agent_name in _STRUCTURED_OPEN_SOURCE_AGENTS:
        return "structured_open_source"
    if agent_name in _LEGACY_VERBOSE_AGENTS:
        return "legacy_verbose"
    return None


# Claude Code: mandatory dependency-style plan (schema aligned with benchmark unit DAG JSON).
_CLAUDE_CODE_PLAN_REQUIREMENT = """

## Planning artifact (required — Claude Code)

You **must** write `/workspace/agent_plans/plan.json` **before** making substantive edits,
and keep it updated if your understanding of dependencies changes.

Use JSON in a **unit-DAG** shape (dependency graph over requirement slugs). The
placeholder ids below are illustrative only — your `nodes[].id` values **must**
match real slugs from `/workspace/requirements/*.yaml` (filename without
`.yaml`).

Abstract example (replace every `id` / `from` / `to` with your task’s slugs):

```json
{
  "repo_id": "<short_task_label>",
  "total_units": 3,
  "num_layers": 2,
  "nodes": [
    {"id": "req_slug_a", "layer": 0, "has_tests": true, "label": "optional human note"},
    {"id": "req_slug_b", "layer": 1, "has_tests": false},
    {"id": "req_slug_c", "layer": 1, "has_tests": true}
  ],
  "edges": [
    {
      "from": "req_slug_a",
      "to": "req_slug_b",
      "type": "module_dependency",
      "label": "why b depends on a"
    },
    {
      "from": "req_slug_b",
      "to": "req_slug_c",
      "type": "module_dependency",
      "label": "optional rationale"
    }
  ]
}
```

Schema rules:

- Optional top-level metadata: `repo_id` (string), `total_units` (int),
  `num_layers` (int).
- **`nodes`**: array of objects. Each node **must** include:
  - `id` (string): equal to a requirement slug — the basename of a file under
    `/workspace/requirements/` without the `.yaml` extension.
  - `layer` (int): topological layer / wave (0 = no inward edges from other
    requirements in this plan, or use increasing layers along dependencies).
  - Optional: `has_tests` (bool), `label` (string).
- **`edges`**: array of objects. Each edge **must** include:
  - `from`, `to` (strings): node `id` values.
  - Optional: `type` (e.g. `module_dependency`), `label` (why this edge exists).

Do not leave `/workspace/agent_plans` empty: `plan.json` must exist and be
valid JSON when the harness collects artifacts.
""".rstrip()


def _benchmark_contract(agent_name: AgentName | None) -> str:
    chunks = [_BENCHMARK_INTRO]
    if agent_name != AgentName.CLAUDE_CODE:
        chunks.append(_OPTIONAL_PLANNING_ARTIFACT)
    chunks.append(_BENCHMARK_FROM_GIT)
    return "\n\n".join(chunks)


def compose_instruction(
    instruction: str,
    agent_name: AgentName | None,
) -> str:
    """Compose the final instruction passed to an agent."""
    if agent_name == AgentName.ORACLE:
        return instruction

    parts = [instruction.rstrip(), _benchmark_contract(agent_name)]
    profile = _resolve_profile(agent_name)
    if profile is not None:
        parts.append(_PROFILE_ADDENDA[profile])
    if agent_name == AgentName.CLAUDE_CODE:
        parts.append(_CLAUDE_CODE_PLAN_REQUIREMENT)
    return "\n\n".join(part for part in parts if part)
