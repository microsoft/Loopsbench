"""Filter nested subprocess pytest lines from meta-runner logs.

When ``test_unit_runner.py`` (or similar) runs an outer pytest session and each
case spawns a *second* pytest inside the repo, failure output often embeds inner
``test/units/...::... FAILED`` lines. The generic pytest line regexes match those
too, inflating ``final_test_results`` beyond the outer session's ``collected N``.

If this heuristic mis-fires for a future task, prefer adding an explicit
``result_sources`` / structured parser, or extend :data:`_META_RUNNER_PREFIXES` /
:data:`_NESTED_DOM_PREFIXES` (or make them configurable via ``task.yaml``).
"""

from __future__ import annotations

from typing import Collection, TypeVar

T = TypeVar("T")

# Outer harness files that wrap repo-native pytest (ansible seg pattern).
_META_RUNNER_PREFIXES: tuple[str, ...] = ("test_unit_runner.py::",)

# Repo-relative pytest nodeids that almost always belong to the inner session
# for ansible-core style trees.
_NESTED_DOM_PREFIXES: tuple[str, ...] = (
    "test/units/",
    "test/integration/",
)


def filter_meta_runner_nested_cases(results: dict[str, T]) -> dict[str, T]:
    """Return *results* without nested dom paths when a meta-runner session is present."""
    if not results or not _cohort_has_meta_runner(results.keys()):
        return results
    return {k: v for k, v in results.items() if not _is_probably_nested_repo_pytest(k)}


def should_skip_nested_pytest_nodeid(nodeid: str, cohort: Collection[str]) -> bool:
    """True if *nodeid* should be ignored when bucketing lines from *cohort*."""
    if not _cohort_has_meta_runner(cohort):
        return False
    return _is_probably_nested_repo_pytest(nodeid)


def _cohort_has_meta_runner(nodeids: Collection[str]) -> bool:
    return any(
        any(n.startswith(prefix) for prefix in _META_RUNNER_PREFIXES)
        for n in nodeids
    )


def _is_probably_nested_repo_pytest(nodeid: str) -> bool:
    return any(nodeid.startswith(p) for p in _NESTED_DOM_PREFIXES)
