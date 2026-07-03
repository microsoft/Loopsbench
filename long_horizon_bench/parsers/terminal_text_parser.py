"""Parser for general terminal text output."""

from __future__ import annotations

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


class TerminalTextParser(BaseParser):
    """Parse terminal text into normalized test results."""

    # Status-first lines must include ``::`` (pytest nodeid); otherwise Django
    # and similar emit ``FAILED (failures=1, skipped=3)`` summary fragments
    # that are not test ids.
    _PYTEST_STATUS_PATTERN = re.compile(
        r"^(PASSED|FAILED|ERROR|SKIPPED)\s+(\S+::\S+)(?:\s+-\s+.*)?$",
        re.MULTILINE,
    )
    _PYTEST_STATUS_PATTERN_V = re.compile(
        r"^((?:\.{0,2}/)?[^\n]*::[^\n]*?)\s+(PASSED|FAILED|ERROR|SKIPPED)(?:\s+\[\s*\d+%\s*\])?\s*$",
        re.MULTILINE,
    )
    _PYTEST_SHORT_SUMMARY_PATTERN = re.compile(
        r"^(FAILED|ERROR|SKIPPED)\s+((?:\.{0,2}/)?[^\n]*::[^\n]*?)(?:\s+-\s+.*)?$",
        re.MULTILINE,
    )
    _CASE_START_PATTERN = re.compile(r"^---\s+(.+?)\s+---\s*$", re.MULTILINE)
    _SUMMARY_PATTERN = re.compile(r"=+\s*(.*?)\s*=+\s*$", re.MULTILINE)
    _GO_CASE_PATTERN = re.compile(
        r"^---\s+(PASS|FAIL|SKIP):\s+(.+?)(?:\s+\([^\n]+\))?\s*$",
        re.MULTILINE,
    )
    _JEST_CASE_PATTERN = re.compile(
        r"^\s*([✓✔✕✗])\s+(.+?)(?:\s+\((?:[0-9]+(?:\.[0-9]+)?)(?:\s*(?:ms|s))\))?\s*$",
        re.MULTILINE,
    )
    _MAVEN_FAILURE_PATTERN = re.compile(
        r"^\[ERROR\]\s+([^\s]+)\s+.*?<<<\s+(FAILURE|ERROR)!?\s*$",
        re.MULTILINE,
    )
    _PHPUNIT_OK_PATTERN = re.compile(r"^OK\s+\(.+tests?.*\)\s*$", re.MULTILINE)
    _PHPUNIT_FAILURES_PATTERN = re.compile(r"^FAILURES!\s*$", re.MULTILINE)
    _RAILS_SUMMARY_PATTERN = re.compile(
        r"^Finished in .*,\s*\d+ runs?,\s*\d+ assertions?,\s*(\d+) failures?,\s*(\d+) errors?(?:,\s*(\d+) skips?)?\.\s*$",
        re.MULTILINE,
    )

    _STATUS_ALIASES = {
        "passed": UnitTestStatus.PASSED,
        "通过": UnitTestStatus.PASSED,
        "failed": UnitTestStatus.FAILED,
        "失败": UnitTestStatus.FAILED,
        "error": UnitTestStatus.ERROR,
        "skipped": UnitTestStatus.SKIPPED,
    }
    _GO_STATUS_MAP = {
        "PASS": UnitTestStatus.PASSED,
        "FAIL": UnitTestStatus.FAILED,
        "SKIP": UnitTestStatus.SKIPPED,
    }
    _JEST_STATUS_MAP = {
        "✓": UnitTestStatus.PASSED,
        "✔": UnitTestStatus.PASSED,
        "✕": UnitTestStatus.FAILED,
        "✗": UnitTestStatus.FAILED,
    }
    _MAVEN_STATUS_MAP = {
        "FAILURE": UnitTestStatus.FAILED,
        "ERROR": UnitTestStatus.ERROR,
    }
    _CASE_STATUS_PATTERN = re.compile(
        rf"^\s*\[({'|'.join(re.escape(alias) for alias in sorted(_STATUS_ALIASES, key=len, reverse=True))})\]\s+(.+?)\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    _PASSED_SUMMARY_TOKENS = {
        alias for alias, status in _STATUS_ALIASES.items() if status == UnitTestStatus.PASSED
    }
    _FAILED_SUMMARY_TOKENS = {
        alias for alias, status in _STATUS_ALIASES.items() if status == UnitTestStatus.FAILED
    }
    _ERROR_SUMMARY_TOKENS = {
        alias for alias, status in _STATUS_ALIASES.items() if status == UnitTestStatus.ERROR
    }

    def parse(self, context: ParseContext) -> ParsedResultSet:
        output = normalize_terminal_text(context.read_text())
        results, confidence, is_summary_only = self._extract_results(output)
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
        return ParsedResultSet(
            source_name=context.source_name,
            format=context.format,
            cases=cases,
            summary_counts=summary_counts,
            raw_artifact_path=context.artifact_path,
            is_fallback=context.is_fallback,
            is_summary_only=is_summary_only,
            confidence=confidence,
        )

    def _extract_results(
        self,
        output: str,
    ) -> tuple[dict[str, UnitTestStatus], str, bool]:
        results: dict[str, UnitTestStatus] = {}
        results.update(self._extract_pytest_style_results(output))
        results.update(self._extract_shell_runner_results(output))
        tail = extract_pytest_tail_session(output)
        if tail is not None:
            results.update(tail)
        if results:
            conf = "case-level+pytest-tail" if tail else "case-level"
            return results, conf, False

        native_results, native_confidence, native_is_summary_only = (
            self._extract_native_framework_results(output)
        )
        if native_results:
            return native_results, native_confidence, native_is_summary_only

        summary_results = self._extract_summary_result(output)
        if summary_results:
            return summary_results, "summary", True
        return {}, "case-level", False

    def _extract_pytest_style_results(self, output: str) -> dict[str, UnitTestStatus]:
        results: dict[str, UnitTestStatus] = {}

        for match in self._PYTEST_STATUS_PATTERN.finditer(output):
            status_str, test_name = match.groups()
            results[test_name.strip()] = self._STATUS_ALIASES.get(status_str.lower(), UnitTestStatus.ERROR)

        # Always run the status-after-name pattern too; -v outputs and
        # tmux-padded outputs (after normalization) emit name-then-status, while
        # short summaries emit status-first. A mix is common in real runs.
        for match in self._PYTEST_STATUS_PATTERN_V.finditer(output):
            test_name, status_str = match.groups()
            results[test_name.strip()] = self._STATUS_ALIASES.get(status_str.lower(), UnitTestStatus.ERROR)

        for match in self._PYTEST_SHORT_SUMMARY_PATTERN.finditer(output):
            status_str, test_name = match.groups()
            results[test_name.strip()] = self._STATUS_ALIASES.get(status_str.lower(), UnitTestStatus.ERROR)

        return filter_meta_runner_nested_cases(results)

    def _extract_shell_runner_results(self, output: str) -> dict[str, UnitTestStatus]:
        results: dict[str, UnitTestStatus] = {}
        case_starts = {match.group(1).strip() for match in self._CASE_START_PATTERN.finditer(output)}
        if not case_starts:
            return results

        for match in self._CASE_STATUS_PATTERN.finditer(output):
            status_str, test_name = match.groups()
            normalized_name = test_name.strip()
            if normalized_name not in case_starts and not any(
                start.endswith(normalized_name) or normalized_name.endswith(start)
                for start in case_starts
            ):
                continue
            results[normalized_name] = self._STATUS_ALIASES.get(status_str.lower(), UnitTestStatus.ERROR)

        return results

    def _extract_native_framework_results(
        self,
        output: str,
    ) -> tuple[dict[str, UnitTestStatus], str, bool]:
        go_results = self._extract_go_results(output)
        if go_results:
            return go_results, "case-level", False

        jest_results = self._extract_jest_results(output)
        if jest_results:
            return jest_results, "case-level", False

        maven_results = self._extract_maven_results(output)
        if maven_results:
            return maven_results, "partial-case-level", False

        phpunit_or_rails_summary = self._extract_phpunit_or_rails_summary(output)
        if phpunit_or_rails_summary:
            return phpunit_or_rails_summary, "summary", True

        return {}, "case-level", False

    def _extract_go_results(self, output: str) -> dict[str, UnitTestStatus]:
        results: dict[str, UnitTestStatus] = {}
        for match in self._GO_CASE_PATTERN.finditer(output):
            status_str, test_name = match.groups()
            results[test_name.strip()] = self._GO_STATUS_MAP[status_str]
        return results

    def _extract_jest_results(self, output: str) -> dict[str, UnitTestStatus]:
        results: dict[str, UnitTestStatus] = {}
        for match in self._JEST_CASE_PATTERN.finditer(output):
            symbol, test_name = match.groups()
            results[test_name.strip()] = self._JEST_STATUS_MAP[symbol]
        return results

    def _extract_maven_results(self, output: str) -> dict[str, UnitTestStatus]:
        results: dict[str, UnitTestStatus] = {}
        for match in self._MAVEN_FAILURE_PATTERN.finditer(output):
            test_name, status_str = match.groups()
            results[test_name.strip()] = self._MAVEN_STATUS_MAP[status_str]
        return results

    def _extract_phpunit_or_rails_summary(
        self,
        output: str,
    ) -> dict[str, UnitTestStatus]:
        if self._PHPUNIT_OK_PATTERN.search(output):
            return {"__summary__": UnitTestStatus.PASSED}
        if self._PHPUNIT_FAILURES_PATTERN.search(output):
            return {"__summary__": UnitTestStatus.FAILED}

        match = self._RAILS_SUMMARY_PATTERN.search(output)
        if not match:
            return {}
        failures, errors, _skips = match.groups()
        if int(failures) == 0 and int(errors) == 0:
            return {"__summary__": UnitTestStatus.PASSED}
        return {"__summary__": UnitTestStatus.FAILED}

    def _extract_summary_result(self, output: str) -> dict[str, UnitTestStatus]:
        # Walk summary blocks bottom-up and pick the first one that mentions
        # any test-result counts (e.g. "5 passed, 1 failed in 0.3s"). The
        # previous implementation always took the *last* `=== ... ===` block,
        # which often is "=== warnings summary ===" or
        # "=== short test summary info ===" — both lack pass/fail counts and
        # were silently being treated as FAILED.
        summary_matches = list(self._SUMMARY_PATTERN.finditer(output))
        if not summary_matches:
            return {}
        for match in reversed(summary_matches):
            summary = match.group(1).lower()
            has_pass = any(token in summary for token in self._PASSED_SUMMARY_TOKENS)
            has_fail = any(token in summary for token in self._FAILED_SUMMARY_TOKENS)
            has_error = any(token in summary for token in self._ERROR_SUMMARY_TOKENS)
            if not (has_pass or has_fail or has_error):
                continue
            if not re.search(r"\b\d+\b", summary):
                continue
            if has_pass and not has_fail and not has_error:
                return {"__summary__": UnitTestStatus.PASSED}
            return {"__summary__": UnitTestStatus.FAILED}
        return {}
