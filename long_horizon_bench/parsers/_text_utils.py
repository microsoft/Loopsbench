"""Shared text normalization helpers for terminal/test output parsers."""
from __future__ import annotations

import re

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# tmux capture-pane often elides newlines and pads each visual row to the
# pane width with spaces. The result is a single ~6MB blob with very few
# real newlines, where each "row" is separated from the next by a long run
# of spaces. Collapse such runs back into a newline so line-anchored regexes
# can find the row boundaries.
_TMUX_PAD_RE = re.compile(r" {4,}")


def normalize_terminal_text(text: str) -> str:
    """Strip ANSI escapes, normalize line endings, recover tmux row breaks.

    Without this:
    - ANSI color codes (default for pytest under TTY/tmux) defeat
      ``^(PASSED|...)`` style regexes.
    - tmux pane captures merge rows into one huge line padded with spaces,
      which both hides row boundaries and triggers catastrophic backtracking
      in lazy regexes like ``(.+?)\\s+(PASSED|...)\\s*$``.
    """
    if not text:
        return text
    text = _ANSI_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _TMUX_PAD_RE.sub("\n", text)
    return "\n".join(line.rstrip() for line in text.split("\n"))
