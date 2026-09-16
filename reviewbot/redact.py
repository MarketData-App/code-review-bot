"""One place that removes token-shaped strings from text.

The token lives in github.py and never reaches the brief, the log or the
comment. This is the second line of defence: a pull request body, a diff or a
model answer is attacker-controlled text, and the bot echoes all three.
"""

import re

# The last alternative is a JWT, and it is here because of the borrowed Codex
# credential: this bot now writes an `auth.json` holding a bearer `access_token`
# and `id_token` onto a runner the model can read, so a model answer or a log
# line can carry one.
#
# It anchors on `eyJ` -- the base64url of `{"`, the first two characters of
# every JWT's JSON header -- rather than matching any three dotted base64url
# runs. The loose form matches ordinary dotted prose and identifiers
# (`reviewbot.credentials.release`), and a redactor that eats the diff it is
# quoting gets deleted rather than fixed.
_TOKEN_RE = re.compile(
    r"\b(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
)


def scrub(text: str) -> str:
    """Replace anything token-shaped with `[redacted]`."""
    return _TOKEN_RE.sub("[redacted]", text or "")
