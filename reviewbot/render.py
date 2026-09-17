"""The review comment: one per pull request, edited in place.

Disabled sections do not render at all, so a repo that turns ratings off gets
a comment with no empty heading where the ratings used to be.
"""

from reviewbot import markers
from reviewbot.findings import by_severity
from reviewbot.policy import Decisions
from reviewbot.redact import scrub as _scrub
from reviewbot.result import overall_rating, tier_word

VERDICT_HEADLINE = {
    "ready": "✅ Ready",
    "needs_changes": "🟡 Needs changes",
    "blocked": "⛔ Blocked",
}

SEVERITY_HEADING = {"blocking": "Blocking", "should_fix": "Should fix", "nit": "Nit"}

# Built, not written: a literal triple backtick in this file would close the
# fence of the code block this module is quoted in.
FENCE = "`" * 3


def check_title(decisions: Decisions) -> str:
    """The one-line title of the check run."""
    return VERDICT_HEADLINE[decisions.verdict]


def error_comment_summary(message: str) -> str:
    """The check-run summary used when the bot itself failed."""
    return (
        "The code review bot did not finish.\n\n"
        f"{FENCE}\n{_scrub(message)[:1500]}\n{FENCE}\n\n"
        "The pull request is not judged. Re-run the job from the Actions tab."
    )


def _location(finding: dict, meta: dict) -> str:
    if not finding.get("file") or not finding.get("line_start"):
        return ""
    url = (
        f"https://github.com/{meta['repo']}/blob/{meta['reviewed_sha']}/"
        f"{finding['file']}#L{finding['line_start']}"
    )
    span = str(finding["line_start"])
    if finding.get("line_end") and finding["line_end"] != finding["line_start"]:
        span = f"{finding['line_start']}-{finding['line_end']}"
    return f"[`{finding['file']}:{span}`]({url})"


def _tags(finding: dict, meta: dict) -> str:
    tags = [finding["category"], f"`{finding['id']}`"]
    if len(meta["backends"]) > 1:
        raised = finding.get("backends") or meta["backends"]
        tags.append("both" if len(raised) > 1 else raised[0])
    return ", ".join(tags)


def _finding_lines(finding: dict, meta: dict, waived: dict) -> list[str]:
    location = _location(finding, meta)
    head = f"- {location} **{_scrub(finding['title'])}** ({_tags(finding, meta)})"
    if not location:
        head = f"- **{_scrub(finding['title'])}** ({_tags(finding, meta)})"
    lines = [head, f"  {_scrub(finding['body'])}"]
    if finding.get("evidence"):
        lines.append(f"  > {_scrub(finding['evidence'])}")
    if finding["id"] in (waived or {}):
        lines.append(f"  _waived by {waived[finding['id']]}_")
    return lines


def render(
    result: dict, meta: dict, policy: dict, decisions: Decisions, since: dict, waived: dict
) -> str:
    """The whole comment body, markers included."""
    waived = waived or {}
    out: list[str] = [f"## {VERDICT_HEADLINE[decisions.verdict]} — Code review", ""]
    out += [_scrub(result["summary"]), ""]

    if decisions.reasons:
        out += [f"- {_scrub(reason)}" for reason in decisions.reasons] + [""]

    if meta.get("unseen_files"):
        listed = ", ".join(f"`{p}`" for p in meta["unseen_files"][:10])
        more = " and more" if len(meta["unseen_files"]) > 10 else ""
        out += [f"The diff exceeded the cap, so these files were not read: {listed}{more}.", ""]

    if policy["ratings"]:
        rating = result["rating"]
        overall = overall_rating(rating)
        # The same words the engine frame gave the model, so the row and the
        # instructions can never drift apart.
        out += [
            f"**Rating** patch {rating['patch']}/6 ({tier_word('patch', rating['patch'])}) · "
            f"proof {rating['proof']}/6 ({tier_word('proof', rating['proof'])}) · "
            f"overall {overall}/6",
            "",
        ]

    if result.get("decision") and policy["decision_packets"]:
        decision = result["decision"]
        out += ["### Decision needed", "", f"**{_scrub(decision['question'])}**", ""]
        out += [f"- {_scrub(option)}" for option in decision["options"]]
        out += ["", f"Recommended: {_scrub(decision['recommendation'])}", ""]

    open_findings = [f for f in result["findings"] if f["id"] not in waived]
    enforced = [
        f for f in open_findings if not policy["require_agreement"] or f.get("agreed", True)
    ]
    blocking = [f for f in enforced if f["severity"] == "blocking"]
    proof = result["proof"]
    proof_ask = proof.get("ask") if proof["status"] in ("missing", "insufficient") else None

    if blocking or proof_ask:
        out += ["### Before merge", ""]
        for finding in blocking:
            location = _location(finding, meta)
            prefix = f"{location} — " if location else ""
            out.append(f"- [ ] {prefix}{_scrub(finding['title'])} (`{finding['id']}`)")
        if proof_ask:
            out.append(f"- [ ] Proof: {_scrub(proof_ask)}")
        out.append("")

    if result["findings"]:
        agreed_findings = [
            f
            for f in result["findings"]
            if not policy["require_agreement"] or f.get("agreed", True)
        ]
        lone = [f for f in result["findings"] if f not in agreed_findings]
        if agreed_findings:
            out += ["### Findings", ""]
            for severity, items in by_severity(agreed_findings).items():
                out += [f"**{SEVERITY_HEADING[severity]}**", ""]
                for finding in items:
                    out += _finding_lines(finding, meta, waived)
                out.append("")
        if lone:
            out += [
                "### One reviewer noted",
                "",
                "Raised by one backend only, so it does not affect the check.",
                "",
            ]
            for finding in lone:
                out += _finding_lines(finding, meta, waived)
            out.append("")

    if since.get("resolved") or since.get("still_open"):
        out += ["### Since last review", ""]
        if since.get("resolved"):
            out.append("- Resolved: " + ", ".join(f"`{i}`" for i in since["resolved"]))
        out.append(
            f"- {len(since.get('still_open', []))} still open, {len(since.get('new', []))} new"
        )
        out.append("")

    ran = ", ".join(f"{name} ({meta['models'].get(name, '?')})" for name in meta["backends"])
    missing = "".join(f" · {name} unavailable" for name in meta.get("missing_backends", []))
    out += [
        "---",
        f"Reviewed `{meta['reviewed_sha'][:7]}` · revision {meta['revision']} · {ran}{missing}",
    ]

    state = {
        "reviewed_sha": meta["reviewed_sha"],
        "revision": meta["revision"],
        "backends": meta["backends"],
        "proof_status": proof["status"],
        "decision_open": bool(result.get("decision")) and policy["decision_packets"],
        "finding_ids": [f["id"] for f in result["findings"]],
        "waived": waived,
    }
    out.append(markers.emit(state))
    return "\n".join(out)
