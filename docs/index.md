# LoopsBench

LoopsBench evaluates AI coding agents on long-horizon terminal tasks. Each task packages a source workspace, requirements, dependency graphs, Docker execution metadata, and verifiers that distinguish incomplete, partial, and complete solutions.

[Get started](quickstart.md){ .md-button .md-button--primary }
[API reference](api/index.md){ .md-button }

## What LoopsBench Provides

- A Python package and CLI for listing tasks, validating tasks, and running agents.
- Task execution in Docker, with local builds and prebuilt images.
- Built-in adapters for Oracle, Mini SWE-agent, SWE-agent, OpenHands, Claude Code, Cursor, Codex, Qwen Code, and Copilot.
- A GitHub-native contribution workflow for task proposals, task PRs, validation, and trusted publishing.
- Generated API documentation for the modules that power the benchmark harness.

## Benchmark Shape

LoopsBench tasks are meant to capture realistic development arcs. A task can include:

- an agent-visible `base/` workspace
- `task.yaml` metadata and natural-language instructions
- module and unit dependency graphs
- unit-level requirement files
- Docker files for client and tester containers
- public and hidden tests
- an Oracle solution or gold patch used by maintainers for validation

## Paper

Read [LoopsBench: From Harness Engineering to Loop Engineering in Coding Agent Evaluation](https://arxiv.org/abs/2608.00267) on arXiv. When reporting experiments, also record the exact repository commit SHA used.
