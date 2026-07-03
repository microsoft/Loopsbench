"""High-confidence pytest session outcome from the final banner + ``__EXIT__`` lines.

Pytest normally ends with a ``============ ... ============`` line summarizing
counts, followed (in LHB) by ``__EXIT__<code>`` appended by ``TestRunner``.

Parsers merge this into the same case dict as line-level parsing (after
nested-pytest filtering). The synthetic id from :func:`tail_case_id` is added
alongside per-test rows; when present, the harness uses **only** that key for
``is_resolved`` so nested subprocess lines cannot flip resolution, while
``final_test_results`` still lists every parsed per-test outcome.
"""

from __future__ import annotations

import re

from long_horizon_bench.parsers.base_parser import UnitTestStatus

_TAIL_CASE_ID = "__lhb_pytest_session_tail__"

_EXIT_RE = re.compile(r"^__EXIT__(\d+)\s*$")
_BANNER_RE = re.compile(r"^=+\s*(.+?)\s*=+\s*$")
_COUNT_TOKEN = re.compile(
    r"(\d+)\s+(failed|passed|error|skipped|warnings?|deselected)",
    re.IGNORECASE,
)


def tail_case_id() -> str:
    return _TAIL_CASE_ID


def extract_pytest_tail_session(output: str) -> dict[str, UnitTestStatus] | None:
    """If the log ends with pytest's final banner and ``__EXIT__``, return one case."""
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    exit_line = lines[-1]
    banner_line = lines[-2]
    m_exit = _EXIT_RE.match(exit_line)
    if not m_exit:
        return None
    exit_code = int(m_exit.group(1))
    m_banner = _BANNER_RE.match(banner_line)
    if not m_banner:
        return None
    inner = m_banner.group(1)
    if not _COUNT_TOKEN.search(inner):
        return None
    failed = _count_word(inner, "failed")
    errors = _count_word(inner, "error")
    # "warnings" / "deselected" do not imply test failure by themselves
    if exit_code != 0 or failed > 0 or errors > 0:
        return {_TAIL_CASE_ID: UnitTestStatus.FAILED}
    return {_TAIL_CASE_ID: UnitTestStatus.PASSED}


def _count_word(inner: str, kind: str) -> int:
    if kind == "failed":
        pat = r"(\d+)\s+failed\b"
    else:
        pat = r"(\d+)\s+errors?\b"
    total = 0
    for m in re.finditer(pat, inner, flags=re.IGNORECASE):
        total += int(m.group(1))
    return total
