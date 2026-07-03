"""Factory for creating test result parsers."""

from enum import Enum

from long_horizon_bench.parsers.base_parser import BaseParser
from long_horizon_bench.parsers.native_text_parser import NativeTextParser
from long_horizon_bench.parsers.pytest_parser import PytestParser
from long_horizon_bench.parsers.terminal_text_parser import TerminalTextParser
from long_horizon_bench.parsers.workspace_results_json_parser import (
    WorkspaceResultsJsonParser,
)


class ParserName(str, Enum):
    """Legacy parser names retained for task.yaml compatibility."""

    PYTEST = "pytest"
    DJANGO = "django"


class ResultFormat(str, Enum):
    """Supported result source formats."""

    PYTEST_TEXT = "pytest-text"
    WORKSPACE_RESULTS_JSON = "workspace-results-json"
    TERMINAL_TEXT = "terminal-text"
    NATIVE_TEXT = "native-text"


class ParserFactory:
    """Create parser instances from result formats."""

    _PARSERS: dict[ResultFormat, type[BaseParser]] = {
        ResultFormat.PYTEST_TEXT: PytestParser,
        ResultFormat.TERMINAL_TEXT: TerminalTextParser,
        ResultFormat.NATIVE_TEXT: NativeTextParser,
        ResultFormat.WORKSPACE_RESULTS_JSON: WorkspaceResultsJsonParser,
    }

    @classmethod
    def get_parser(cls, result_format: ResultFormat | str) -> BaseParser:
        """Return a parser instance for the given result format."""
        fmt = ResultFormat(result_format)
        parser_cls = cls._PARSERS.get(fmt)
        if parser_cls is None:
            raise ValueError(
                f"Unknown result format: {fmt}. "
                f"Available parsers: {', '.join(p.value for p in ResultFormat)}"
            )
        return parser_cls()
