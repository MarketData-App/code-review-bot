"""One place that removes token-shaped strings from text.

The token lives in github.py and never reaches the brief, the log or the
comment. This is the second line of defence: a pull request body, a diff or a
model answer is attacker-controlled text, and the bot echoes all three.

It is only the second line. A model that wants to leak a credential can encode
it, and no regex will catch that. The org-only gate in spec section 6 is what
covers deliberate exfiltration; this covers the accident.
"""

import re

_PATTERNS = (
    # GitHub tokens: gho_, ghp_, ghs_, ghu_, ghr_, and fine-grained PATs.
    r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,})",
    # OpenAI and Anthropic keys.
    r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}",
    # A JWT, which is what a Codex access token is: three base64url segments,
    # the first of which decodes to a JOSE header, so it always starts "eyJ".
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    # A PEM block. The body is restricted to the base64 alphabet and
    # whitespace, which is what a PEM body actually is, and the END marker is
    # optional so a block truncated by the diff cap or an error tail is still
    # caught.
    #
    # The restriction is load-bearing, not tidiness. An unbounded body under
    # re.DOTALL matched from a stray BEGIN marker forward to the next END
    # marker anywhere in the text: a bare marker in this repository's own test
    # fixtures ate 39% of one pull request's diff and nine whole files, and
    # scrub() runs over the diff the model reviews (brief._fence). Anything
    # outside the base64 alphabet -- a quote, a bracket, a diff's own "-" --
    # now stops the match where it should. The alphabet excludes the space
    # character on purpose: a PEM body has no spaces inside a line, while
    # prose is mostly letters and spaces, so allowing it let a bare marker eat
    # the sentence that followed it.
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[A-Za-z0-9+/=\r\n\t]*"
    r"(?:-----END [A-Z ]*PRIVATE KEY-----)?",
)

_TOKEN_RE = re.compile("|".join(_PATTERNS))


def scrub(text: str) -> str:
    """Replace anything token-shaped with `[redacted]`."""
    return _TOKEN_RE.sub("[redacted]", text or "")
