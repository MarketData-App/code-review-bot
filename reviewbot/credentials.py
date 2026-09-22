"""The Codex credential: derive a copy that cannot refresh, and lease it.

One real `auth.json` lives in a vault on skynet and never moves. What every
review job borrows is a DERIVED copy whose refresh token is a placeholder.
That is the whole security argument: OpenAI's guidance forbids sharing one
`auth.json` across concurrent jobs because two processes redeeming one refresh
token kills the credential (`refresh_token_reused`). A copy that holds no
refresh token cannot redeem one, so no number of concurrent jobs can cause it.

Measured 2026-09-16 with Codex CLI 0.153.4: a hand-written `auth.json` whose
`refresh_token` is a placeholder runs `codex exec` to completion, and does so
even with `last_refresh` 46 days old. `refresh_token` must be PRESENT, because
Codex fails to parse the file without it; it need not be real.
"""

import base64
import binascii
import collections
import copy
import dataclasses
import datetime
import json
import re

PLACEHOLDER = "REVIEWBOT-PLACEHOLDER-NOT-A-REFRESH-TOKEN"

ISSUE_PATH = "codex/auth.json"
META_PATH = "codex/meta.json"
LEASE_PATH = "codex/lease.json"

ISSUE_BRANCH = "issue"
LEASE_BRANCH = "main"


class CredentialError(RuntimeError):
    """The credential could not be read, derived or leased."""


def derive(auth: dict) -> dict:
    """A copy of `auth` that authenticates but cannot refresh.

    Deep-copied, because the keeper derives from the vault it has just read and
    a mutating version would write the placeholder back into the vault.
    """
    tokens = auth.get("tokens")
    if not isinstance(tokens, dict) or not tokens.get("refresh_token"):
        raise CredentialError("the credential has no refresh_token to replace")
    out = copy.deepcopy(auth)
    out["tokens"]["refresh_token"] = PLACEHOLDER
    return out


def access_token_expiry(auth: dict) -> datetime.datetime:
    """When the access token stops working, as an aware UTC datetime.

    The token is never verified here, only read: this decides whether the
    keeper may publish, and GitHub is not the issuer.
    """
    tokens = auth.get("tokens")
    token = tokens.get("access_token") if isinstance(tokens, dict) else None
    if not isinstance(token, str):
        raise CredentialError("the credential has no access_token")
    parts = token.split(".")
    if len(parts) != 3:
        raise CredentialError("the access_token is not a JWT")
    segment = parts[1]
    try:
        payload = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise CredentialError(f"the access_token payload could not be read: {exc}") from exc
    exp = payload.get("exp")
    if not isinstance(exp, int):
        raise CredentialError("the access_token carries no exp claim")
    return datetime.datetime.fromtimestamp(exp, datetime.UTC)


def _read_lease(api) -> tuple[dict, str | None]:
    text, sha = api.file_with_sha(LEASE_PATH, LEASE_BRANCH)
    if text is None:
        return {}, None
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        # A lease nobody can parse must not wedge every review forever. Treat
        # it as free; the write below replaces it with something readable.
        return {}, sha
    return (body if isinstance(body, dict) else {}), sha


def _held(lease: dict, now: datetime.datetime) -> bool:
    """Is this lease still someone else's? An unreadable expiry means no.

    Every branch that cannot read a definite expiry answers "free", for the
    same reason `_read_lease` treats unparseable JSON as free: the design
    promises that a credential problem needs no human action and no recovery
    runbook, and a lease that reads as held forever wedges every review until
    somebody hand-edits a file in the store. The lease is a courtesy, not the
    safety property -- the borrowed credential cannot refresh, so two jobs
    holding it at once is survivable, while a permanently wedged lease is not.

    A naive `expires_at` is read as UTC rather than raising. The bot only ever
    writes an aware, UTC isoformat; a naive one came from a hand edit, and
    comparing it as UTC is both answerable and honest about its intent.
    """
    if not lease.get("holder"):
        return False
    expires = lease.get("expires_at")
    if not isinstance(expires, str):
        return False
    try:
        when = datetime.datetime.fromisoformat(expires)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.UTC)
    return when > now


def lease_message(verb: str, holder: str) -> str:
    """The commit message for a lease write. NEVER the raw holder.

    `holder` is `<owner>/<repo>#<pr>#<run>-<attempt>`, and `<owner>/<repo>#<pr>`
    is exactly GitHub's cross-repository issue reference syntax. Writing it into
    a commit message in the store posted a "referenced this in
    code-review-credentials" event onto the pull request's own timeline -- once
    on borrow and once on return, on every review, for the life of the branch.
    Measured 2026-09-22 on MarketData-App/api#463: twelve lease commits, twelve
    timeline events, timestamps matching to the second.

    So the message spells the same three facts without the `#`. The HOLDER is
    untouched: `release` compares it to decide whether this run still owns the
    lease, and that identity is the whole reason it carries the run id and
    attempt. Only the human-facing text changes.

    `lease_spans` reads both this and the old `#`-joined form, because the
    store holds months of the old one.
    """
    repo, *rest = holder.split("#")
    out = f"lease {verb} by {repo}"
    if len(rest) >= 1 and rest[0]:
        out += f" pull {rest[0]}"
    if len(rest) >= 2 and rest[1]:
        out += f" run {rest[1]}"
    return out


def acquire(api, holder: str, run_url: str, now: datetime.datetime, ttl_minutes: int = 20) -> bool:
    """Take the lease, or return False. Never raises for an ordinary loss.

    The lock is the contents API's own compare-and-swap: the lease is read with
    its blob sha and written back under it, so a second job racing for the same
    lease gets a 409 and loses. No lock service is involved.
    """
    lease, sha = _read_lease(api)
    if _held(lease, now):
        return False
    body = {
        "holder": holder,
        "run_url": run_url,
        "acquired_at": now.isoformat(),
        "expires_at": (now + datetime.timedelta(minutes=ttl_minutes)).isoformat(),
    }
    return api.put_file(
        LEASE_PATH,
        json.dumps(body, indent=2) + "\n",
        lease_message("taken", holder),
        LEASE_BRANCH,
        sha,
    )


def release(api, holder: str) -> None:
    """Free the lease, but only if this exact run still holds it.

    A job whose TTL expired may find another job already holding the lease by
    the time its `if: always()` step runs. Freeing it then would hand a second
    job the credential while the first is still working.

    That guarantee is only as good as `holder`, and `holder` must therefore
    name the RUN, not the pull request. The workflow sets
    `cancel-in-progress: true`, so a push to a pull request routinely leaves a
    cancelled run's check-in racing a new run that has already taken the
    lease. While the holder was `<repo>#<pr>`, those two strings were equal,
    this comparison passed, and the dying run freed the live run's lease --
    the precise case this function exists to prevent. The workflow now appends
    `#<run_id>-<run_attempt>`, so a holder identifies one run and one attempt.
    """
    lease, sha = _read_lease(api)
    if lease.get("holder") != holder:
        return
    body = {"holder": None, "run_url": None, "acquired_at": None, "expires_at": None}
    api.put_file(
        LEASE_PATH,
        json.dumps(body, indent=2) + "\n",
        lease_message("freed", holder),
        LEASE_BRANCH,
        sha,
    )


# --- the lease history -----------------------------------------------------
#
# `acquire` and `release` each write `codex/lease.json`, so the file's commit
# history is a complete record of who held the shared credential and when. It
# is the only durable one: the job log expires with the Actions run, and the
# review comment says `codex unavailable` without ever saying why.
#
# What the history CANNOT show is a run that WAITED. A blocked job writes no
# commit -- it sits in `credential_checkout`'s poll loop printing
# `the credential lease is held` to its own log and nothing else. So this
# module reports occupancy, and `lease_report` points the reader at the log
# string for the question the history cannot answer.

# BOTH MESSAGE FORMATS. The trailing groups are the form `lease_message`
# writes now; a token carrying `#` is the form written before 2026-09-22,
# and the store holds months of it.
_LEASE_MESSAGE = re.compile(r"^lease (taken|freed) by (\S+)(?: pull (\S+))?(?: run (\S+))?$")

# How far BEFORE the reported window to read the history.
#
# A hold that began before the window and ended inside it arrives as a lone
# `lease freed`, and `lease_spans` cannot pair it -- so the hold and the time
# it really occupied both vanish from the report. Reading back far enough to
# carry its `lease taken` fixes that, and `clip` then trims it to the window.
#
# Two hours is an upper bound on one hold, not a guess: `review.yml` caps the
# whole job at 90 minutes, and a lease outlives its holder by at most its TTL
# (`2 * timeout_minutes + 5`, under 45 minutes for the timeout_minutes the
# arithmetic in `_lease_minutes` permits).
HISTORY_MARGIN = datetime.timedelta(hours=2)


@dataclasses.dataclass(frozen=True)
class LeaseSpan:
    """One borrow of the credential, from `lease taken` to `lease freed`."""

    holder: str
    repo: str
    pr: int | None
    run: str
    start: datetime.datetime
    end: datetime.datetime | None

    @property
    def closed(self) -> bool:
        return self.end is not None

    @property
    def seconds(self) -> int:
        """How long it was held. ZERO when it was never freed, never a guess.

        A holder that died leaves the lease to its TTL, and the history does
        not record when the job stopped working. A plausible-looking duration
        here would be the one number a reader takes at face value.
        """
        if self.end is None:
            return 0
        return int((self.end - self.start).total_seconds())


def _parse_holder(holder: str) -> tuple[str, int | None, str]:
    """`owner/name#pr#run-attempt` -> (repo, pr, run).

    The workflow builds this string, so a shape change must degrade rather
    than raise: a report is a diagnostic and must survive the thing it is
    diagnosing.
    """
    parts = holder.split("#")
    repo = parts[0]
    pr = None
    if len(parts) > 1 and parts[1].isdigit():
        pr = int(parts[1])
    run = parts[2] if len(parts) > 2 else ""
    return repo, pr, run


def lease_spans(commits: list[dict]) -> list[LeaseSpan]:
    """Pair every `lease taken` with its `lease freed`, oldest first.

    `commits` arrives newest first, the order the API returns, and is reversed
    here rather than at the call site.

    A `freed` with no open `taken` is dropped: the window's first commit is
    often the release of a borrow that began before it. An unreadable message
    is skipped for the same reason `_held` treats an unparseable lease as free
    -- this must not be the thing that breaks.
    """
    open_spans: dict[tuple, datetime.datetime] = {}
    out: list[LeaseSpan] = []
    for entry in reversed(commits or []):
        body = (entry.get("commit") or {}).get("message") or ""
        match = _LEASE_MESSAGE.match(body.strip().splitlines()[0] if body.strip() else "")
        if not match:
            continue
        verb, token = match.group(1), match.group(2)
        # One key for one hold, whichever format wrote it, so a `taken` and its
        # `freed` pair on identity rather than on spelling.
        if "#" in token:
            repo, pr, run = _parse_holder(token)
            key = (repo, pr, run)
        else:
            repo, run = token, match.group(4) or ""
            raw_pr = match.group(3)
            pr = int(raw_pr) if raw_pr and raw_pr.isdigit() else None
            key = (repo, pr, run)
        stamp = ((entry.get("commit") or {}).get("committer") or {}).get("date")
        try:
            when = datetime.datetime.fromisoformat((stamp or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.UTC)
        if verb == "taken":
            open_spans[key] = when
            continue
        start = open_spans.pop(key, None)
        if start is None:
            continue
        out.append(LeaseSpan(token, repo, pr, run, start, when))
    for (repo, pr, run), start in open_spans.items():
        out.append(LeaseSpan(f"{repo}#{pr}#{run}", repo, pr, run, start, None))
    out.sort(key=lambda s: s.start)
    return out


def clip(
    spans: list[LeaseSpan], start: datetime.datetime, end: datetime.datetime
) -> list[LeaseSpan]:
    """The spans that touch [start, end], trimmed to it.

    The margin above means the history reaches back past the window, so every
    figure in the report would otherwise count time outside the window it
    names. A span entirely outside is dropped; one that straddles a boundary
    keeps only the part inside.

    An unclosed span keeps its open end: it contributes no duration anywhere,
    and it must still be counted as never freed.
    """
    out = []
    for span in spans:
        if span.end is not None and span.end <= start:
            continue
        if span.start >= end:
            continue
        out.append(
            dataclasses.replace(
                span,
                start=max(span.start, start),
                end=None if span.end is None else min(span.end, end),
            )
        )
    return out


def occupied_seconds(spans: list[LeaseSpan]) -> int:
    """How long the credential was busy: the UNION of the holds, not their sum.

    The two differ exactly when holds overlap, which this module reports
    separately and therefore cannot pretend does not happen. Summing instead
    double-counts the overlap and can report a credential busy for more than
    100% of the window -- a figure that discredits the whole report.
    """
    intervals = sorted((s.start, s.end) for s in spans if s.closed)
    total = 0.0
    open_start = open_end = None
    for begins, ends in intervals:
        if open_end is None or begins > open_end:
            if open_end is not None:
                total += (open_end - open_start).total_seconds()
            open_start, open_end = begins, ends
        else:
            open_end = max(open_end, ends)
    if open_end is not None:
        total += (open_end - open_start).total_seconds()
    return int(total)


def overlaps(spans: list[LeaseSpan]) -> int:
    """How many closed holds began while another was still open.

    This is the lease's whole purpose, so it is worth counting. An overlap
    means a TTL expired under a job that was still working. The borrowed
    credential survives it -- a copy that cannot refresh cannot be killed by
    concurrent use, which is the argument `derive` rests on -- but a reader
    chasing a slow review should see that it happened.
    """
    closed = sorted((s for s in spans if s.closed), key=lambda s: s.start)
    count = 0
    for i, span in enumerate(closed):
        if any(other.end > span.start for other in closed[:i]):
            count += 1
    return count


def _hm(seconds: int) -> str:
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _holds(count: int) -> str:
    """`hold` is padded to the width of `holds` so the columns stay aligned."""
    return f"{count:>4} " + ("hold " if count == 1 else "holds")


def lease_report(spans: list[LeaseSpan], store: str, days: int, now: datetime.datetime) -> str:
    """The human-readable report `reviewbot lease-report` prints."""
    window_start = now - datetime.timedelta(days=days)
    spans = clip(spans, window_start, now)
    closed = [s for s in spans if s.closed]
    unclosed = len(spans) - len(closed)
    # `total` is machine time spent holding and double-counts an overlap on
    # purpose: it answers "how much work did the credential carry?".
    # `occupied` answers "how much of the window was it unavailable?" and must
    # count an overlap once. Reporting one number for both questions was wrong.
    total = sum(s.seconds for s in closed)
    occupied = occupied_seconds(spans)
    window = days * 86400
    clashes = overlaps(spans)

    head = (
        f"Codex credential lease · {store}",
        f"{window_start.date()} to {now.date()} ·{_holds(len(spans))} · {_hm(total)} held"
        + (f" · {unclosed} never freed" if unclosed else ""),
        f"The credential was busy {100 * occupied / window:.1f}% of the window."
        + (f" {clashes} hold(s) overlapped another." if clashes else ""),
    )

    by_day = collections.defaultdict(lambda: [0, 0])
    by_repo = collections.defaultdict(lambda: [0, 0])
    for span in spans:
        for bucket in (by_day[span.start.date().isoformat()], by_repo[span.repo]):
            bucket[0] += 1
            bucket[1] += span.seconds

    lines = [*head, "", "By day"]
    for day in sorted(by_day, reverse=True):
        holds, held = by_day[day]
        lines.append(f"  {day}  {_holds(holds)}   {_hm(held)}")
    lines += ["", "By repository"]
    for repo in sorted(by_repo, key=lambda r: (-by_repo[r][1], r)):
        holds, held = by_repo[repo]
        share = f"{100 * held / total:.0f}%" if total else "0%"
        lines.append(f"  {repo:<34}{_holds(holds)}   {_hm(held)}   {share:>4}")

    # THE LIMIT OF THIS REPORT, stated in it rather than in a docstring nobody
    # reading the output will open. A blocked run writes no commit, so no
    # figure above counts one.
    lines += [
        "",
        "This history counts HOLDS, not waits: a run blocked on the lease writes",
        "no commit here. To count those, grep the target repository's Actions log",
        "for `reviewbot: the credential lease is held`, one line per 20s waited,",
        "and `still held after` for a run that gave up.",
    ]
    return "\n".join(lines)
