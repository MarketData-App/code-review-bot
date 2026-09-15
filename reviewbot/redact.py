"""One place that removes token-shaped strings from text.

The token lives in github.py and never reaches the brief, the log or the
comment. This is the second line of defence: a pull request body, a diff or a
model answer is attacker-controlled text, and the bot echoes all three.
"""

import re

_TOKEN_RE = re.compile(
    r"\b(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{20,})"
)


def scrub(text: str) -> str:
    """Replace anything token-shaped with `[redacted]`."""
    return _TOKEN_RE.sub("[redacted]", text or "")
