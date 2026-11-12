"""The hidden state the bot carries inside its own comment.

There is no database. Everything the next run needs to say "resolved / still
open / new" travels in two HTML comments at the foot of the review comment.
"""

import json
import re

MARKER = "<!-- marketdata-code-review -->"

# The login GitHub gives this App's own comments. A person's login cannot hold
# brackets, and another App has another slug, so the login says who wrote it.
BOT_LOGIN = "marketdata-code-review[bot]"

_STATE_RE = re.compile(r"<!--\s*reviewbot-state:\s*(\{.*?\})\s*-->")


def emit(state: dict) -> str:
    """The marker line and the state line, both HTML comments."""
    payload = json.dumps(state, separators=(",", ":"), sort_keys=True)
    return f"{MARKER}\n<!-- reviewbot-state: {payload} -->"


def parse(body: str | None) -> dict:
    """Read the state out of a comment body. Unreadable state reads as empty.

    The last match wins: a human who quotes an older review inside a reply
    must not send the bot back in time.
    """
    if not body:
        return {}
    matches = _STATE_RE.findall(body)
    for raw in reversed(matches):
        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(state, dict):
            return state
    return {}


def is_bot_comment(body: str | None) -> bool:
    """True when this comment is the bot's own review comment."""
    return bool(body) and MARKER in body


def is_own_comment(comment: dict | None) -> bool:
    """True when this App wrote `comment` and it carries the marker.

    `comment` is an issue comment as the API returns it. Anyone who can
    comment can paste the marker, so the body alone cannot say whose review it
    is; the author's login can. Raises nothing.
    """
    if not comment or not is_bot_comment(comment.get("body")):
        return False
    return ((comment.get("user") or {}).get("login") or "").lower() == BOT_LOGIN
