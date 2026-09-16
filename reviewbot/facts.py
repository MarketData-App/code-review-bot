"""What the engine knows about a pull request, and the pure helpers that shape it.

This module holds no I/O. github.py fills PRFacts in; policy.py, brief.py and
render.py read it, and none of them needs to import the module that holds the
token.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

_FILE_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)


@dataclass(frozen=True)
class PRFacts:
    """One pull request, as the engine sees it."""

    number: int
    title: str
    body: str
    author: str
    author_is_bot: bool
    draft: bool
    labels: list[str]
    head_sha: str
    base_ref: str
    node_id: str
    # GitHub's own word for the author's relationship to the repository. The
    # org-only gate reads it; it is set by GitHub, not by the pull request.
    author_association: str
    changed_files: list[dict]
    diff: str
    unseen_files: list[str]
    ci_state: str
    previous_comment: dict | None
    # What CI said, fetched by the harness so the model never has to look.
    check_results: list[dict] = field(default_factory=list)
    previous_state: dict = field(default_factory=dict)
    comments_since: list[dict] = field(default_factory=list)

    @property
    def paths(self) -> list[str]:
        return [f["path"] for f in self.changed_files]


def cap_diff(
    diff: str, max_kb: int, ignore_paths: list[str] | None = None
) -> tuple[str, list[str]]:
    """Keep whole per-file sections up to the budget; name the files dropped.

    A file section is never split. The model must not reason about half a hunk
    and report the other half as missing.

    Files matching `ignore_paths` are dropped before the budget is counted,
    and are not reported as unseen: the repository has said it does not want
    them reviewed, so they must not spend the budget or block `ready`. Without
    this, one large ignored fixture hides every file after it and the pull
    request can never be ready, because allow_ready_with_unseen_files is
    false by default.
    """
    if not diff:
        return "", []
    budget = max_kb * 1024
    headers = list(_FILE_HEADER.finditer(diff))
    if not headers:
        return diff, []

    sections = []
    preamble = diff[: headers[0].start()]
    for index, match in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(diff)
        sections.append((match.group(2), diff[match.start() : end]))

    kept = [preamble]
    used = len(preamble.encode("utf-8"))
    unseen = []
    for path, text in sections:
        if matches_any(path, ignore_paths or []):
            continue
        size = len(text.encode("utf-8"))
        # Once one file is dropped, every later file is dropped too. A diff
        # that skips a file in the middle reads as if that file were unchanged.
        if unseen or used + size > budget:
            unseen.append(path)
            continue
        kept.append(text)
        used += size
    return "".join(kept), unseen


CI_DIR = ".reviewbot-ci"


def write_check_logs(results: list[dict], checkout: str) -> list[dict]:
    """Write each check's full log to a file the reviewer can grep.

    The model gets a PATH, not a wall of text. It has Read, Grep and Glob, so it
    can go looking when a finding depends on what CI said, and pays tokens only
    for what it actually reads. Trimming or extracting on its behalf meant
    guessing which lines matter, and a measurement showed the guess was wrong:
    on a real 84,870 byte job log the pytest summary sat 53,681 bytes from the
    END, after coverage upload, codecov and post-action cleanup.

    The files live under a directory named for this bot, INSIDE the checkout,
    because that is the one tree both backends can read. The brief says plainly
    that they are bot-provided and not part of the diff.
    """
    out: list[dict] = []
    base = Path(checkout) / CI_DIR
    for item in results:
        entry = {k: v for k, v in item.items() if k != "log"}
        entry["log_path"] = ""
        log = item.get("log") or ""
        if log:
            slug = re.sub(r"[^A-Za-z0-9._-]+", "-", item.get("name", "check")).strip("-").lower()
            try:
                base.mkdir(parents=True, exist_ok=True)
                target = base / f"{slug or 'check'}.log"
                target.write_text(log, encoding="utf-8")
                entry["log_path"] = f"{CI_DIR}/{target.name}"
                entry["log_bytes"] = len(log)
            except OSError:
                # A log we cannot write is a log the reviewer does without.
                entry["log_path"] = ""
        out.append(entry)
    return out


def _translate(pattern: str) -> re.Pattern:
    """Translate a path glob to a regex. `**` crosses directories; `*` does not."""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[":
            close = pattern.find("]", i + 1)
            if close == -1:
                out.append(re.escape("["))
                i += 1
            else:
                body = pattern[i + 1 : close]
                body = "^" + body[1:] if body.startswith("!") else body
                out.append(f"[{body}]")
                i = close + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


_cache: dict[str, re.Pattern] = {}


def path_matches(path: str, pattern: str) -> bool:
    """True when `path` matches the glob `pattern`."""
    if pattern not in _cache:
        _cache[pattern] = _translate(pattern)
    return bool(_cache[pattern].match(path))


def matches_any(path: str, patterns: list[str]) -> bool:
    """True when `path` matches at least one pattern. No patterns means no match."""
    return any(path_matches(path, p) for p in patterns or [])


def all_match(paths: list[str], patterns: list[str]) -> bool:
    """True when every path matches. An empty pattern list means nobody, never everybody."""
    if not patterns or not paths:
        return False
    return all(matches_any(p, patterns) for p in paths)
