"""The engine's decisions. Pure functions, no I/O, no model.

The model reports what it found. Everything that changes the state of a pull
request is decided here, where it can be read and tested without a network.
"""

import re
from dataclasses import dataclass, field

from reviewbot import findings as findings_mod
from reviewbot.facts import PRFacts, all_match, matches_any

HANDLE = "@marketdata-code-review"

LABEL_READY = "review: ready"
LABEL_CHANGES = "review: needs changes"
LABEL_PROOF = "review: needs proof"
LABEL_DECISION = "review: decision needed"
OWNED_LABELS = (LABEL_READY, LABEL_CHANGES, LABEL_PROOF, LABEL_DECISION)

# A maintainer label the bot honours but never sets or clears.
PROOF_WAIVED_LABEL = "review: proof waived"

_WAIVE_RE = re.compile(rf"{re.escape(HANDLE)}\s+waive\s+([0-9a-f]{{8}}(?:\s+[0-9a-f]{{8}})*)", re.I)


@dataclass(frozen=True)
class Decisions:
    """What the publisher must do, and why."""

    conclusion: str
    verdict: str
    labels_add: list[str] = field(default_factory=list)
    labels_remove: list[str] = field(default_factory=list)
    approve: bool = False
    auto_merge: str = "leave"
    reasons: list[str] = field(default_factory=list)


def parse_author_list(raw: str | None) -> list[str]:
    """Split a comma-separated author list, tolerating spaces and empties.

    The workflow and the engine must agree on what a list means, so there is
    one parser and both use it.
    """
    return [name.strip() for name in (raw or "").split(",") if name.strip()]


def is_trusted(author: str, association: str, policy: dict) -> bool:
    """True when this author may have their pull request reviewed at all.

    The bot reviews the organisation's own pull requests and nobody else's.
    A model on the self-hosted runner can read any file on that machine
    (spec section 6), so this is the security boundary, not a preference.
    """
    listed = {name.strip().lower() for name in (policy["trusted_authors"] or [])}
    if (author or "").strip().lower() in listed:
        return True
    return (association or "").upper() in (policy["trusted_associations"] or [])


def should_skip(pr: PRFacts, policy: dict, force: bool = False) -> str | None:
    """A reason to write nothing at all, or None to review.

    Trust is NOT decided here. It needs an API call that this module must not
    make, and deciding it from `author_association` alone is what let the gate
    and the run disagree inside one job: the gate asked the API and said
    member, this said COLLABORATOR and skipped. cli.run and cli.gate now share
    one trust function, so the two halves cannot differ.

    Every skip is decided before a model runs, and an ignored PR gets no
    comment and no check run (spec section 5).
    """
    if pr.draft and not policy["review_drafts"]:
        return "the pull request is a draft"
    if pr.author in policy["ignore_authors"]:
        return f"the author {pr.author} is in ignore_authors"
    if "[skip review]" in pr.title.lower():
        return "the title carries [skip review]"
    if not pr.changed_files:
        return "the pull request changes no files"
    if all(matches_any(path, policy["ignore_paths"]) for path in pr.paths):
        return "every changed file matches ignore_paths"
    # A commit reviewed once does not need reviewing again. `same_sha` in
    # cli.run already noticed this, but only to reuse the revision NUMBER --
    # the model still ran, spent tokens and rewrote the same comment. Harmless
    # while the only trigger was a manual dispatch; not harmless once
    # `pull_request_target` is armed, because its `edited` type fires on a
    # title or description tweak at an unchanged commit.
    #
    # Two things still force a review: asking for one, and `force`. Asking
    # matters most for `waive` -- without it a waiver could not take effect
    # until the author happened to push.
    if not force and pr.previous_state.get("reviewed_sha") == pr.head_sha:
        if not any(is_bot_command(c.get("body", "")) for c in pr.comments_since or []):
            return "this commit has already been reviewed"
    return None


def proof_applies(pr: PRFacts, policy: dict) -> bool:
    """True when the proof gate covers this pull request.

    `proof.paths` is an ANY-match over the whole pull request, not a per-file
    filter: one matching file turns the gate on for the entire change. It
    exists as a floor under the model's judgement, so a pull request that
    touches nothing matching cannot be blocked for missing proof even when the
    model mislabels it. Contrast `auto_approve_paths`, which is ALL-match.
    """
    if not policy["proof"]["required"]:
        return False
    paths = policy["proof"]["paths"]
    if not paths:
        return True
    return any(matches_any(path, paths) for path in pr.paths)


def wants_rereview(body: str, handle: str = HANDLE) -> bool:
    """True when a PR comment asks the bot for another pass."""
    text = " ".join((body or "").split()).lower()
    return text.startswith(handle.lower()) and "re-review" in text


# The commands spec section 8 defines. A comment addressed to the bot that
# carries neither is conversation, not an instruction, and must not start a
# job on the runner.
COMMANDS = ("re-review", "waive")


def is_bot_command(body: str, handle: str = HANDLE) -> bool:
    """True when a comment addresses the bot AND carries a defined command.

    `re-review` is not the only one: `waive` must start a run too, or the
    waiver only takes effect on the author's next push. Anything else
    addressed to the bot is ignored, so saying thank you costs nothing.
    """
    text = " ".join((body or "").split()).lower()
    if not text.startswith(handle.lower()):
        return False
    rest = text[len(handle) :].strip()
    return any(rest.startswith(command) for command in COMMANDS)


def collect_waivers(comments: list[dict], previous: dict, handle: str = HANDLE) -> dict:
    """Finding ids a maintainer has waived, mapped to who waived them.

    Waivers are cumulative: one recorded in an earlier revision stays waived.
    """
    waived = dict(previous or {})
    pattern = (
        _WAIVE_RE
        if handle == HANDLE
        else re.compile(
            rf"{re.escape(handle)}\s+waive\s+([0-9a-f]{{8}}(?:\s+[0-9a-f]{{8}})*)", re.I
        )
    )
    for comment in comments or []:
        if not comment.get("is_maintainer"):
            continue
        for match in pattern.finditer(comment.get("body") or ""):
            for token in match.group(1).split():
                waived[token.lower()] = comment.get("author", "a maintainer")
    return waived


def decide(result: dict, pr: PRFacts, policy: dict, waived: dict) -> Decisions:
    """Turn one merged result plus the PR facts into everything the bot does."""
    reasons: list[str] = []
    verdict = result["verdict"]["value"]

    open_findings = findings_mod.drop_waived(result["findings"], list(waived or {}))
    blocking = [f for f in open_findings if f["severity"] == "blocking"]
    if policy["require_agreement"]:
        # In `all` mode a finding only one backend raised is reported, not enforced.
        # A single-backend run marks every finding agreed, so this is a no-op there.
        blocking = [f for f in blocking if f.get("agreed", True)]
    if blocking and verdict == "ready":
        verdict = "needs_changes"
        reasons.append(f"{len(blocking)} blocking finding(s) remain open")

    proof_status = result["proof"]["status"]
    proof_unmet = proof_status in ("missing", "insufficient") and proof_applies(pr, policy)
    if proof_unmet and PROOF_WAIVED_LABEL in pr.labels:
        reasons.append("the proof gate is waived by a maintainer label")
        proof_unmet = False
    if proof_unmet:
        verdict = "blocked"
        reasons.append("runtime evidence is missing for this change")

    if policy["require_ci_green"] and pr.ci_state == "failure":
        verdict = "blocked"
        reasons.append("CI is red on the head commit")

    if pr.unseen_files and not policy["allow_ready_with_unseen_files"] and verdict == "ready":
        verdict = "needs_changes"
        reasons.append(
            "the diff cap hid "
            + ", ".join(pr.unseen_files[:5])
            + (" and more" if len(pr.unseen_files) > 5 else "")
        )

    has_decision = bool(result.get("decision")) and policy["decision_packets"]

    if verdict == "ready":
        conclusion = "success"
    elif verdict == "blocked":
        conclusion = "failure" if policy["gate"] else "neutral"
    else:
        conclusion = "neutral"

    labels_add = []
    if verdict == "ready":
        labels_add.append(LABEL_READY)
    else:
        labels_add.append(LABEL_CHANGES)
    if proof_unmet:
        labels_add.append(LABEL_PROOF)
    if has_decision:
        labels_add.append(LABEL_DECISION)
    labels_remove = [x for x in OWNED_LABELS if x not in labels_add]

    approve_paths = policy["auto_approve_paths"]
    approve = bool(
        policy["auto_approve"]
        and verdict == "ready"
        and (not approve_paths or all_match(pr.paths, approve_paths))
    )

    auto_merge = "leave"
    if policy["auto_merge"]["enabled"]:
        blockers = []
        if verdict != "ready":
            blockers.append("the verdict is not ready")
        if proof_unmet:
            blockers.append("proof is outstanding")
        if has_decision:
            blockers.append("a decision is open")
        if pr.ci_state != "success":
            blockers.append(f"CI is {pr.ci_state}")
        if pr.author not in policy["auto_merge"]["authors"]:
            blockers.append(f"the author {pr.author} is not on the auto-merge list")
        if blockers:
            auto_merge = "disarm"
            reasons.append("auto-merge stays off: " + "; ".join(blockers))
        else:
            auto_merge = "arm"

    return Decisions(
        conclusion=conclusion,
        verdict=verdict,
        labels_add=labels_add,
        labels_remove=labels_remove,
        approve=approve,
        auto_merge=auto_merge,
        reasons=reasons,
    )
