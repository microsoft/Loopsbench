"""Parser for pytest-like text output."""

import re

from long_horizon_bench.harness.models import ParsedResultSet, TestCaseResult
from long_horizon_bench.parsers._pytest_nested_filter import filter_meta_runner_nested_cases
from long_horizon_bench.parsers._pytest_tail_session import extract_pytest_tail_session
from long_horizon_bench.parsers._text_utils import normalize_terminal_text
from long_horizon_bench.parsers.base_parser import (
    BaseParser,
    ParseContext,
    UnitTestStatus,
    normalize_test_case_id,
)


class PytestParser(BaseParser):
    """Parse pytest-like text output into normalized test results."""

    _STATUS_PATTERN = re.compile(
        r"^(PASSED|FAILED|ERROR|SKIPPED)\s+(\S+::\S+)(?:\s+-\s+.*)?$",
        re.MULTILINE,
    )
    _STATUS_PATTERN_V = re.compile(
        r"^((?:\.{0,2}/)?[^\n]*::[^\n]*?)\s+(PASSED|FAILED|ERROR|SKIPPED)(?:\s+\[\s*\d+%\s*\])?\s*$",
        re.MULTILINE,
    )
    _SHORT_SUMMARY_PATTERN = re.compile(
        r"^(FAILED|ERROR|SKIPPED)\s+((?:\.{0,2}/)?[^\n]*::[^\n]*?)(?:\s+-\s+.*)?$",
        re.MULTILINE,
    )
    _SUMMARY_PATTERN = re.compile(r"=+\s*(.*?)\s*=+\s*$", re.MULTILINE)

    _STATUS_MAP = {
        "PASSED": UnitTestStatus.PASSED,
        "FAILED": UnitTestStatus.FAILED,
        "ERROR": UnitTestStatus.ERROR,
        "SKIPPED": UnitTestStatus.SKIPPED,
    }

    def parse(self, context: ParseContext) -> ParsedResultSet:
        """Parse pytest-like text output into a normalized result set."""
        output = normalize_terminal_text(context.read_text())
        results: dict[str, UnitTestStatus] = {}

        for match in self._STATUS_PATTERN.finditer(output):
            status_str, test_name = match.groups()
            results[test_name.strip()] = self._STATUS_MAP.get(
                status_str, UnitTestStatus.ERROR
            )

        # Always run V (status-after-name) too. Some pytest configurations and
        # short-summary lines use forward order; -v output uses reverse order;
        # tmux-padded outputs (post-normalization) also tend to be reverse. A
        # mix is common, so we union both.
        for match in self._STATUS_PATTERN_V.finditer(output):
            test_name, status_str = match.groups()
            results[test_name.strip()] = self._STATUS_MAP.get(
                status_str, UnitTestStatus.ERROR
            )

        # Short summary (FAILED ...) is authoritative for failures and runs
        # last so it can promote a previously-PASSED case to FAILED if pytest
        # reran or rerolled it.
        for match in self._SHORT_SUMMARY_PATTERN.finditer(output):
            status_str, test_name = match.groups()
            results[test_name.strip()] = self._STATUS_MAP.get(
                status_str, UnitTestStatus.ERROR
            )

        results = filter_meta_runner_nested_cases(results)
        tail = extract_pytest_tail_session(output)
        if tail is not None:
            results.update(tail)

        # Intentionally NO __summary__ fallback here. Previously a parser miss
        # (e.g. ANSI-colored output, no captured case lines) would fall through
        # to scanning `=== ... ===` blocks and emit {"__summary__": PASSED} when
        # the last block happened to contain the word "passed". That silently
        # turned real failures into resolved trials and vice versa. Better to
        # return zero cases and let the harness mark the trial unresolved /
        # parser_failed than to fabricate a result.

        cases = [
            TestCaseResult(
                id=normalize_test_case_id(test_name),
                status=status,
                source=context.source_name,
            )
            for test_name, status in results.items()
        ]
        summary_counts: dict[str, int] = {}
        for status in results.values():
            summary_counts[status.value] = summary_counts.get(status.value, 0) + 1
        if tail is not None:
            confidence = "case-level+pytest-tail"
        else:
            confidence = "case-level" if results else "parser_failed"
        return ParsedResultSet(
            source_name=context.source_name,
            format=context.format,
            cases=cases,
            summary_counts=summary_counts,
            raw_artifact_path=context.artifact_path,
            is_fallback=context.is_fallback,
            is_summary_only=False,
            confidence=confidence,
        )
