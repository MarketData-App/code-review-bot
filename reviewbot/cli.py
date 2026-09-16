"""`reviewbot run`: the order of operations for one review.

Read the policy from the base branch, gather the facts, decide whether to
review at all, compose the brief, run the backends, merge, decide in pure
code, then publish. A failure of the bot itself is reported on the check run
as `neutral`, never as a red pull request.
"""

import argparse
import dataclasses
import json
import os
import sys
import traceback

from reviewbot import brief as brief_mod
from reviewbot import config, findings, merge, render
from reviewbot import policy as policy_mod
from reviewbot import result as result_mod
from reviewbot.backends import base as backends
from reviewbot.github import GitHub, GitHubError
from reviewbot.redact import scrub

CONFIG_DIR = ".github/code-review"
POLICY_PATH = f"{CONFIG_DIR}/policy.yml"
REVIEW_PATH = f"{CONFIG_DIR}/REVIEW.md"

ANNOTATION_LEVEL = {"blocking": "failure", "should_fix": "warning", "nit": "notice"}

APPROVAL_BODY = "The code review bot found nothing blocking and the evidence is sufficient."


def pr_number_from_event(event: dict) -> int | None:
    """The pull request this event is about, or None when there is nothing to do."""
    if "pull_request" in event and isinstance(event["pull_request"], dict):
        number = event["pull_request"].get("number")
        if number:
            return int(number)
    issue = event.get("issue") or {}
    if issue.get("pull_request"):
        body = (event.get("comment") or {}).get("body", "")
        # Any command addressed to the bot, not only `re-review`: `waive`
        # must start a run too, or the waiver waits for the author's next push.
        if policy_mod.is_bot_command(body):
            return int(issue["number"])
        return None
    inputs = event.get("inputs") or {}
    if inputs.get("pr"):
        return int(inputs["pr"])
    return None


def refused(reason: str) -> dict:
    """A decided refusal: this pull request may not be reviewed."""
    return {"trusted": False, "undecided": False, "reason": reason}


def undecided(reason: str) -> dict:
    """We could not decide. The step fails, so the silence is visible."""
    return {"trusted": False, "undecided": True, "reason": reason}


def gate(
    *,
    event: dict,
    repo: str,
    token: str,
    api=None,
    org_api=None,
    trusted_authors: str = "",
    pr_number: int | None = None,
) -> dict:
    """Decide whether this pull request may be reviewed, before any checkout.

    The review job's `if:` expression is a cheap first filter. It cannot see
    the pull request's author on an `issue_comment` or a `workflow_dispatch`
    event -- those payloads carry the commenter, or nothing at all -- so on
    its own it would let an org member aim the bot at an outsider's fork and
    fetch it onto the persistent runner. This runs before that fetch, and it
    always judges the pull request's own author.

    `org_api` is a second client, authenticated as the ORGANISATION's App
    installation, and it answers the membership question. Measured on
    2026-09-15: asking GET /orgs/MarketData-App/members/MarketDataDev02 with
    the MarketData-App installation returns 204, and with the MarketDataApp
    user-account installation returns 404 -- not 403. A user-account
    installation has no `members` permission at all and GitHub answers as
    though the person were a stranger, so the repository's own token cannot be
    trusted with this question on a user-account repo, which is where most of
    the review volume lives.

    Returns {"trusted": bool, "undecided": bool, "reason": str}. "Refused" and
    "could not decide" are different outcomes: the first is the gate working,
    the second must fail the step rather than skip the review in silence.
    """
    number = pr_number or pr_number_from_event(event)
    if number is None:
        return refused("the event names no pull request to review")

    api = api or GitHub(repo, token)

    try:
        pull = api.pull_request(number)
    except GitHubError as exc:
        return undecided(f"could not read the pull request: {exc}")

    base_ref = (pull.get("base") or {}).get("ref") or "HEAD"
    try:
        policy = config.load(api.file_at_ref(POLICY_PATH, base_ref))
    except config.PolicyError:
        # A malformed policy must not widen the gate. The run itself reports
        # the error; here we simply fall back to the defaults.
        policy = config.defaults()
    except GitHubError as exc:
        return undecided(f"could not read {POLICY_PATH}: {exc}")

    policy["trusted_authors"] = list(
        dict.fromkeys(policy["trusted_authors"] + policy_mod.parse_author_list(trusted_authors))
    )

    # An issue_comment must satisfy both: the commenter asks for the work, and
    # the pull request's author decides whether it may happen at all. The
    # commenter is judged against the repository's own policy, not the
    # defaults, so a repo that widens the list widens it for both.
    comment = event.get("comment") or {}
    if comment:
        commenter_login = (comment.get("user") or {}).get("login", "")
        commenter_assoc = comment.get("author_association") or ""
        if _trust(org_api or api, policy, commenter_login, commenter_assoc) is None:
            return refused(
                f"the commenter {commenter_login or '(unknown)'} is not in the "
                f"{policy['organisation']} organisation"
            )

    author = (pull.get("user") or {}).get("login", "")
    association = pull.get("author_association", "")
    verdict = _trust(org_api or api, policy, author, association)
    if verdict is not None:
        return verdict
    return refused(
        f"{author} is not in the {policy['organisation']} organisation "
        f"(association {association or 'NONE'})"
    )


def _trust(api, policy: dict, login: str, association: str) -> dict | None:
    """Trusted? Returns the decision, or None when the answer is "no".

    Order matters. A named bot is trusted outright, because a bot account is
    never an organisation member. Otherwise organisation membership decides:
    it is the thing the operator actually means by "us", and unlike
    author_association it does not change with which account owns the repo.
    Only when membership cannot be determined does author_association get a
    say, so a missing App permission degrades to the old behaviour instead of
    refusing everyone.
    """
    listed = {name.strip().lower() for name in (policy["trusted_authors"] or [])}
    if (login or "").strip().lower() in listed:
        return {
            "trusted": True,
            "undecided": False,
            "reason": f"{login} is trusted: on the trusted-authors list",
        }

    org = policy["organisation"]
    member = api.is_org_member(org, login) if org else None
    if member is True:
        return {
            "trusted": True,
            "undecided": False,
            "reason": f"{login} is a member of the {org} organisation",
        }
    if member is False:
        return None

    # Could not tell. Fall back to the association, and say so.
    if (association or "").upper() in policy["trusted_associations"]:
        return {
            "trusted": True,
            "undecided": False,
            "reason": f"{login} is trusted by association {association} "
            f"(membership in {org} could not be checked)",
        }
    return None


def annotations_for(findings_list: list[dict]) -> list[dict]:
    """Located findings become check-run annotations, inline on the diff."""
    out = []
    for item in findings_list:
        if not item.get("file") or not item.get("line_start"):
            continue
        start = int(item["line_start"])
        end = int(item.get("line_end") or start)
        out.append(
            {
                "path": item["file"],
                "start_line": start,
                "end_line": max(start, end),
                "annotation_level": ANNOTATION_LEVEL[item["severity"]],
                "title": scrub(item["title"])[:255],
                "message": scrub(item["body"])[:64000],
            }
        )
    return out


def _fail(api, sha: str, check_name: str, message: str) -> int:
    """Report a bot failure and leave the pull request's verdict alone."""
    try:
        api.write_check_run(
            sha, check_name, "neutral", "Bot error", render.error_comment_summary(message), []
        )
    except GitHubError:
        pass
    print(f"reviewbot: {scrub(message)}", file=sys.stderr)
    return 1


def run(
    *,
    event: dict,
    repo: str,
    token: str,
    checkout: str,
    pr_number: int | None = None,
    api=None,
    org_api=None,
) -> int:
    """One whole review. Returns the process exit code."""
    number = pr_number or pr_number_from_event(event)
    if number is None:
        print("reviewbot: nothing to review for this event")
        return 0

    api = api or GitHub(repo, token)
    head_sha = ""
    check_name = config.defaults()["check_name"]

    try:
        pull = api.pull_request(number) if hasattr(api, "pull_request") else None
        base_ref = (pull or {}).get("base", {}).get("ref", "") if pull else ""
    except GitHubError:
        base_ref = ""

    try:
        # The base branch, never the head: a pull request cannot rewrite its
        # own review rules.
        policy = config.load(api.file_at_ref(POLICY_PATH, base_ref or "HEAD"))
        check_name = policy["check_name"]
        review_md = brief_mod.load_review(api.file_at_ref(REVIEW_PATH, base_ref or "HEAD"))
    except config.PolicyError as exc:
        pr = _safe_gather(api, number, config.defaults())
        return _fail(
            api, pr.head_sha if pr else "", check_name, f"{POLICY_PATH} is malformed: {exc}"
        )
    except GitHubError as exc:
        return _fail(api, "", check_name, f"could not read {CONFIG_DIR}: {exc}")

    try:
        pr = api.gather(number, policy["max_diff_kb"], check_name, policy["ignore_paths"])
        head_sha = pr.head_sha
    except GitHubError as exc:
        return _fail(api, "", check_name, f"could not read the pull request: {exc}")

    # The workflow's own gate has already refused an outsider, so the job
    # never started. This is the backstop for a misconfigured caller: the
    # same allow list, delivered by the workflow, merged with the repo's.
    from_env = policy_mod.parse_author_list(os.environ.get("REVIEWBOT_TRUSTED_AUTHORS", ""))
    if from_env:
        policy["trusted_authors"] = list(dict.fromkeys(policy["trusted_authors"] + from_env))

    # The same trust function the gate uses, with the same client. Deciding it
    # twice from different evidence is what made one job say "is a member of
    # the MarketData-App organisation" and "is not in the organisation"
    # seconds apart (sdk-py run 35022651232).
    if _trust(org_api or api, policy, pr.author, pr.author_association) is None:
        # The gate refuses this before the pull request head is fetched, so
        # reaching it here means the caller is misconfigured -- most likely the
        # run step is missing REVIEWBOT_ORG_TOKEN. A policy skip is silent by
        # design; a trust failure must not look the same.
        return _fail(
            api,
            pr.head_sha,
            check_name,
            f"{pr.author} was refused by the trust check inside `reviewbot run`, "
            f"after the gate had already allowed the review. The two disagree only "
            f"when they see different evidence: check that the `Run the review` "
            f"step passes REVIEWBOT_ORG_TOKEN.",
        )

    skip = policy_mod.should_skip(pr, policy)
    if skip:
        print(f"reviewbot: skipped, {skip}")
        return 0

    try:
        text = brief_mod.compose(pr, policy, review_md, checkout)
        results, missing = backends.run(policy, text, checkout)
        merger = (
            merge.model_merger(backends.build(results[0].backend, policy, checkout))
            if len(results) > 1
            else None
        )
        merged = merge.merge(results, policy, unlocated_merger=merger)
    except backends.BackendError as exc:
        return _fail(api, head_sha, check_name, str(exc))
    except result_mod.ResultError as exc:
        return _fail(api, head_sha, check_name, str(exc))

    waived = policy_mod.collect_waivers(pr.comments_since, pr.previous_state.get("waived", {}))
    decisions = policy_mod.decide(merged, pr, policy, waived)
    since = findings.since_last_review(pr.previous_state.get("finding_ids", []), merged["findings"])

    previous_revision = int(pr.previous_state.get("revision", 0))
    same_sha = pr.previous_state.get("reviewed_sha") == pr.head_sha
    revision = previous_revision if (same_sha and previous_revision) else previous_revision + 1

    meta = {
        "repo": repo,
        "reviewed_sha": pr.head_sha,
        "revision": revision,
        "backends": [r.backend for r in results],
        "models": {r.backend: r.model for r in results},
        "missing_backends": missing,
        "unseen_files": pr.unseen_files,
    }

    extra_notes = _apply_auto_merge(api, pr, policy, decisions)
    if extra_notes:
        decisions = dataclasses.replace(decisions, reasons=decisions.reasons + extra_notes)

    body = render.render(merged, meta, policy, decisions, since, waived)

    try:
        api.upsert_review_comment(number, body)
        api.write_check_run(
            pr.head_sha,
            check_name,
            decisions.conclusion,
            render.check_title(decisions),
            scrub(merged["summary"]),
            annotations_for(merged["findings"]),
        )
        api.apply_labels(number, decisions.labels_add, decisions.labels_remove, pr.labels)
        if decisions.approve:
            api.approve(number, APPROVAL_BODY)
    except GitHubError as exc:
        return _fail(api, pr.head_sha, check_name, f"could not publish the review: {exc}")

    print(f"reviewbot: {decisions.verdict} on {repo}#{number} at {pr.head_sha[:7]}")
    return 0


def _apply_auto_merge(api, pr, policy, decisions) -> list[str]:
    """Arm or disarm native auto-merge. A failure here never fails the review.

    Reading a branch protection rule would need an Administration permission
    the App does not have, so the bot arms and reports what GitHub says. The
    one prerequisite it can see for free is `allow_auto_merge` on the
    repository object; when that field is absent it arms anyway.
    """
    if decisions.auto_merge == "leave":
        return []
    try:
        if decisions.auto_merge == "arm":
            if _auto_merge_forbidden(api):
                return [
                    'auto-merge stays off: "Allow auto-merge" is disabled in the '
                    "repository settings"
                ]
            api.set_auto_merge(pr.node_id, policy["auto_merge"]["method"])
        else:
            api.clear_auto_merge(pr.node_id)
        return []
    except GitHubError as exc:
        return [f"auto-merge could not be changed: {exc}"]


def _auto_merge_forbidden(api) -> bool:
    """True only when the repository says outright that auto-merge is off."""
    try:
        return api.repository().get("allow_auto_merge") is False
    except GitHubError:
        return False


def _safe_gather(api, number: int, policy: dict):
    try:
        return api.gather(number, policy["max_diff_kb"], policy["check_name"])
    except Exception:
        return None


def derive_credential(codex_home: str, out: str, min_hours: float) -> int:
    """Write the copy every review job borrows. Runs on skynet, in the keeper.

    It refuses rather than publishing a credential that will expire during a
    review: a missing copy degrades to a Claude-only review, and an expiring
    one fails in the middle of a job.
    """
    import datetime as _dt
    from pathlib import Path

    from reviewbot import credentials

    source = Path(codex_home) / "auth.json"
    try:
        auth = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"reviewbot derive-credential: cannot read {source}: {exc}")
        return 1

    try:
        expires = credentials.access_token_expiry(auth)
        copy_out = credentials.derive(auth)
    except credentials.CredentialError as exc:
        print(f"reviewbot derive-credential: {exc}")
        return 1

    now = _dt.datetime.now(_dt.UTC)
    left = (expires - now).total_seconds() / 3600
    if left < min_hours:
        print(
            f"reviewbot derive-credential: refusing to publish, the access token "
            f"expires in {left:.1f}h ({expires.isoformat()}), under the {min_hours}h floor"
        )
        return 1

    target = Path(out)
    target.mkdir(parents=True, exist_ok=True)
    auth_path = target / "auth.json"
    auth_path.write_text(json.dumps(copy_out))
    auth_path.chmod(0o600)
    (target / "meta.json").write_text(
        json.dumps({"expires_at": expires.isoformat(), "published_at": now.isoformat()}, indent=2)
        + "\n"
    )
    print(f"reviewbot derive-credential: published, the access token expires {expires.isoformat()}")
    return 0


def _store_api(repo: str, token: str):
    """The store is a different repository, so it needs its own client.

    A seam, so the tests can inject a transport without reaching the network.
    """
    return GitHub(repo, token)


def _say_output(key: str, value: str) -> None:
    """Print for a human, and write for the workflow step that reads it."""
    print(f"{key}={value}")
    out_path = os.environ.get("GITHUB_OUTPUT")
    if out_path:
        with open(out_path, "a") as handle:
            handle.write(f"{key}={value}\n")


def credential_checkout(
    store: str, holder: str, run_url: str, codex_home: str, attempts: int, token: str
) -> int:
    """Take the lease and write the borrowed credential. Never fails the review.

    Every failure here reports `fetched=false` and exits 0. A job that cannot
    borrow the credential must review with Claude alone, not go red: a missing
    backend is already an ordinary outcome for `backends.run()`.
    """
    import datetime as _dt
    import time as _time
    from pathlib import Path

    from reviewbot import credentials

    api = _store_api(store, token)
    for attempt in range(1, max(1, attempts) + 1):
        try:
            if credentials.acquire(api, holder, run_url, _dt.datetime.now(_dt.UTC)):
                break
            print(f"reviewbot: the credential lease is held, attempt {attempt}/{attempts}")
        except GitHubError as exc:
            print(f"reviewbot: cannot reach the credential store: {scrub(str(exc))}")
            _say_output("fetched", "false")
            return 0
        if attempt == attempts:
            _say_output("fetched", "false")
            return 0
        _time.sleep(min(2**attempt, 30))

    try:
        text = api.file_at_ref(credentials.ISSUE_PATH, credentials.ISSUE_BRANCH)
    except GitHubError as exc:
        print(f"reviewbot: cannot read the issued credential: {scrub(str(exc))}")
        credentials.release(api, holder)
        _say_output("fetched", "false")
        return 0
    if not text:
        print("reviewbot: the store holds no issued credential")
        credentials.release(api, holder)
        _say_output("fetched", "false")
        return 0

    # Mask before writing: a later step that echoes the file must not leak it.
    try:
        for value in (json.loads(text).get("tokens") or {}).values():
            if isinstance(value, str) and value != credentials.PLACEHOLDER:
                print(f"::add-mask::{value}")
    except json.JSONDecodeError:
        pass

    home = Path(codex_home)
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    auth = home / "auth.json"
    auth.write_text(text)
    auth.chmod(0o600)
    _say_output("fetched", "true")
    return 0


def credential_checkin(store: str, holder: str, codex_home: str, token: str) -> int:
    """Delete the borrowed credential, then free the lease. Never fails.

    In that order, deliberately. The lease expires by itself after its TTL; a
    credential left behind on a persistent self-hosted runner does not.
    """
    import shutil as _shutil
    from pathlib import Path

    from reviewbot import credentials

    _shutil.rmtree(Path(codex_home), ignore_errors=True)
    try:
        credentials.release(_store_api(store, token), holder)
    except GitHubError as exc:
        print(f"reviewbot: could not free the lease, it will expire: {scrub(str(exc))}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """`reviewbot run --event <path> [--pr N]`."""
    parser = argparse.ArgumentParser(prog="reviewbot")
    sub = parser.add_subparsers(dest="command", required=True)
    runner = sub.add_parser("run", help="review one pull request")
    runner.add_argument(
        "--event",
        default=os.environ.get("GITHUB_EVENT_PATH"),
        help="path to the GitHub event payload",
    )
    runner.add_argument("--pr", type=int, default=None, help="pull request number")
    runner.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name of the repository to review",
    )
    runner.add_argument(
        "--checkout", required=True, help="path to the read-only checkout of the pull request head"
    )

    gater = sub.add_parser(
        "gate", help="decide whether this pull request may be reviewed, before any checkout"
    )
    gater.add_argument(
        "--event",
        default=os.environ.get("GITHUB_EVENT_PATH"),
        help="path to the GitHub event payload",
    )
    gater.add_argument("--pr", type=int, default=None, help="pull request number")
    gater.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name of the repository",
    )

    deriver = sub.add_parser(
        "derive-credential", help="write the derived copy the review jobs borrow"
    )
    deriver.add_argument("--codex-home", required=True, help="the vault's CODEX_HOME")
    deriver.add_argument("--out", required=True, help="where to write auth.json and meta.json")
    deriver.add_argument(
        "--min-hours",
        type=float,
        default=48.0,
        help="refuse to publish a token with less life than this",
    )

    checkout = sub.add_parser("credential-checkout", help="borrow the Codex credential")
    checkout.add_argument("--store", required=True, help="owner/name of the credential store")
    checkout.add_argument("--holder", required=True, help="who is taking the lease")
    checkout.add_argument("--run-url", default="", help="the run that holds the lease")
    checkout.add_argument("--codex-home", required=True, help="where to write auth.json")
    checkout.add_argument("--attempts", type=int, default=4, help="lease attempts before giving up")

    checkin = sub.add_parser("credential-checkin", help="return the Codex credential")
    checkin.add_argument("--store", required=True, help="owner/name of the credential store")
    checkin.add_argument("--holder", required=True, help="who took the lease")
    checkin.add_argument("--codex-home", required=True, help="the directory to remove")
    args = parser.parse_args(argv)

    if args.command == "derive-credential":
        return derive_credential(args.codex_home, args.out, args.min_hours)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("REVIEWBOT_TOKEN")
    if not token:
        parser.error("GITHUB_TOKEN is not set")

    if args.command == "credential-checkout":
        return credential_checkout(
            args.store, args.holder, args.run_url, args.codex_home, args.attempts, token
        )
    if args.command == "credential-checkin":
        return credential_checkin(args.store, args.holder, args.codex_home, token)

    if not args.repo:
        parser.error("GITHUB_REPOSITORY is not set and --repo was not given")

    event = {}
    if args.event and os.path.exists(args.event):
        with open(args.event) as handle:
            event = json.load(handle)

    if args.command == "gate":
        org_token = os.environ.get("REVIEWBOT_ORG_TOKEN")
        decision = gate(
            event=event,
            repo=args.repo,
            token=token,
            org_api=GitHub(args.repo, org_token) if org_token else None,
            trusted_authors=os.environ.get("REVIEWBOT_TRUSTED_AUTHORS", ""),
            pr_number=args.pr,
        )
        verdict = "true" if decision["trusted"] else "false"
        print(f"reviewbot gate: trusted={verdict} - {scrub(decision['reason'])}")
        out_path = os.environ.get("GITHUB_OUTPUT")
        if out_path:
            with open(out_path, "a") as handle:
                handle.write(f"trusted={verdict}\n")
        # A refusal is the gate working, and exits 0: the job stops quietly and
        # writes no comment and no check run. Not being able to decide is a
        # different thing, and fails the step so the silence is visible.
        return 1 if decision.get("undecided") else 0

    try:
        org_token = os.environ.get("REVIEWBOT_ORG_TOKEN")
        return run(
            event=event,
            repo=args.repo,
            token=token,
            checkout=args.checkout,
            pr_number=args.pr,
            org_api=GitHub(args.repo, org_token) if org_token else None,
        )
    except Exception:  # the job must fail loudly, never silently
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
