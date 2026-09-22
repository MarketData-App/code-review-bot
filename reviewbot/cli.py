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
import time as _time
import traceback

from reviewbot import brief as brief_mod
from reviewbot import config, facts, findings, merge, render
from reviewbot import policy as policy_mod
from reviewbot import result as result_mod
from reviewbot.backends import base as backends
from reviewbot.config import JOB_BUDGET_MINUTES, JOB_OVERHEAD_MINUTES
from reviewbot.github import GitHub, GitHubError
from reviewbot.redact import scrub

CONFIG_DIR = ".github/code-review"
POLICY_PATH = f"{CONFIG_DIR}/policy.yml"
REVIEW_PATH = f"{CONFIG_DIR}/REVIEW.md"

ANNOTATION_LEVEL = {"blocking": "failure", "should_fix": "warning", "nit": "notice"}

APPROVAL_BODY = "The code review bot found nothing blocking and the evidence is sufficient."


def pr_number_from_event(event: dict) -> int | None:
    """The pull request this event is about, or None when there is nothing to do.

    A `workflow_run` event is how a review gets triggered by CI FINISHING rather
    than by the push that starts it. `pull_request_target` fires on `opened` and
    `synchronize`, which is the moment CI starts -- and since the review skips a
    commit whose checks are still running, an automatic review would skip and
    never return. Measured on sdk-py: CI takes ~75s, a review reaches the gate
    in 30-40.

    GitHub leaves `workflow_run.pull_requests` EMPTY for a fork's pull request,
    so this returns None there rather than guessing; the caller resolves it by
    head sha with an API call, which this module must not make.
    """
    run = event.get("workflow_run")
    if isinstance(run, dict):
        for item in run.get("pull_requests") or []:
            if isinstance(item, dict) and item.get("number"):
                return int(item["number"])
        return None
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


def _resolve_pr(event: dict, api, pr_number: int | None) -> int | None:
    """The pull request to act on, including the fork case a payload cannot answer.

    `workflow_run.pull_requests` is empty for a fork's pull request, so the head
    sha is looked up here -- in the layer that is allowed to call the API.
    """
    number = pr_number or pr_number_from_event(event)
    if number:
        return number
    run = event.get("workflow_run")
    if isinstance(run, dict) and run.get("head_sha") and api is not None:
        return api.pull_for_sha(str(run["head_sha"]))
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
    api = api or GitHub(repo, token)
    number = _resolve_pr(event, api, pr_number)
    if number is None:
        return refused("the event names no pull request to review")

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
    force: bool = False,
) -> int:
    """One whole review. Returns the process exit code."""
    api = api or GitHub(repo, token)
    number = _resolve_pr(event, api, pr_number)
    if number is None:
        print("reviewbot: nothing to review for this event")
        return 0

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
        review = brief_mod.resolve_review(api.file_at_ref(REVIEW_PATH, base_ref or "HEAD"))
        review_md = review.text
    except config.PolicyError as exc:
        pr = _safe_gather(api, number, config.defaults())
        return _fail(
            api, pr.head_sha if pr else "", check_name, f"{POLICY_PATH} is malformed: {exc}"
        )
    except brief_mod.ReviewError as exc:
        # A typo in an include silently dropped the rules it named. Fail with
        # the name, the way a malformed policy key does.
        pr = _safe_gather(api, number, config.defaults())
        return _fail(
            api, pr.head_sha if pr else "", check_name, f"{REVIEW_PATH} is malformed: {exc}"
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

    skip = policy_mod.should_skip(pr, policy, force=force)
    if skip:
        print(f"reviewbot: skipped, {skip}")
        return 0

    # Write CI's logs where the reviewer can grep them. After should_skip, so a
    # pull request that is not green never gets this far and never costs the
    # requests: by the time we are here every check has already passed.
    pr = dataclasses.replace(pr, check_results=facts.write_check_logs(pr.check_results, checkout))

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
        "rule_sets": review.includes,
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
    # A vault file may be valid JSON that is not an object (e.g. "5" or
    # "null"), so guard with isinstance rather than trust `.get` to exist.
    # This runs unattended in the keeper: it owes one line and exit 1, not a
    # traceback. `credential_checkout` guards the store's reply the same way.
    if not isinstance(auth, dict):
        print(f"reviewbot derive-credential: {source} is not a JSON object")
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
    # The directory, not only the file: a 0600 auth.json inside a 0755
    # directory still tells every other account on the machine that it is
    # there, and `credential_checkout` already holds $CODEX_HOME at 0700.
    target.chmod(0o700)
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


def _target_policy(repo: str, pr_number: int, token: str) -> dict | None:
    """Will this repository's policy actually REACH the codex backend?

    Membership in `backends` is not the question, and answering it that way was
    a real defect: the shipped default is `backends: [claude, codex]` with
    `mode: first`, and `backends/base.py` runs only `ready[0]` in that mode, so
    Codex is built, probed and never invoked. A guard that asked only "is codex
    listed" returned True for every repository on the defaults, and each would
    take the single org-wide lease for a whole review while running no Codex --
    starving the repositories that genuinely use it, which now WAIT for the
    lease rather than giving up on it.

    The one case this cannot see is liveness: under `mode: first` an earlier
    backend with no credential would fall through to Codex. Deciding that here
    would mean probing credentials before the lease, so the trade is deliberate
    -- a repository that lists Codex second under `mode: first` and loses its
    first backend reviews without Codex instead of borrowing.

    Fails TOWARD borrowing: an unnecessary borrow wastes a lease slot for a few
    minutes, a missed one silently drops a backend the repository asked for.
    """
    from reviewbot import config

    try:
        api = _store_api(repo, token)
        pull = api.pull_request(pr_number) or {}
        base_ref = (pull.get("base") or {}).get("ref") or "HEAD"
        policy = config.load(api.file_at_ref(POLICY_PATH, base_ref))
    except Exception as exc:  # noqa: BLE001 - never fail the review over a probe
        print(
            f"reviewbot: could not read {repo}'s policy "
            f"({type(exc).__name__}: {scrub(str(exc))}); borrowing anyway"
        )
        return None

    return policy


def _will_run_codex(policy: dict | None) -> bool:
    """Will the policy actually REACH the codex backend? None means unknown."""
    if policy is None:
        return True
    names = policy.get("backends") or []
    if "codex" not in names:
        return False
    # `first` runs ready[0] only; `fallback` reaches a later backend solely when
    # the one before it FAILS, which is rare. In both cases codex listed after
    # another backend is not worth holding the org-wide lease for -- it would
    # starve the repositories that reach codex on every run.
    if policy.get("mode") in ("first", "fallback") and names[0] != "codex":
        return False
    return True


def _lease_minutes(policy: dict | None) -> tuple[float, float]:
    """(ttl, wait), both sized from the policy rather than guessed.

    A backend review is `for attempt in (1, 2)` around a call bounded by
    `timeout_minutes`, so ONE backend can legitimately run for twice that. A
    TTL shorter than the work it protects is worse than no TTL: it expires
    under a job that is still working and hands the credential to a second one.

    So ttl covers the worst case with a margin, and the wait exceeds the ttl --
    that ordering is what lets a waiter outlast a holder that died without
    checking in, instead of giving up just before the lease frees itself.
    """
    cap = float((policy or {}).get("timeout_minutes") or 15)
    ttl = 2 * cap + 5
    wait = ttl + 5
    # The wait must also fit INSIDE the job's own budget alongside the review
    # it is waiting to run. `JOB_BUDGET_MINUTES` mirrors review.yml's
    # timeout-minutes; a policy with a large timeout_minutes would otherwise
    # size a wait that the runner kills mid-queue, which looks like a failed
    # review rather than a busy one.
    # THE TWO GOALS CONFLICT ABOVE A CERTAIN timeout_minutes, and the arithmetic
    # is worth writing down rather than rediscovering. We want
    #   ttl  > 2*cap                 (outlast the work it protects)
    #   wait > ttl                   (outlast a dead holder's lease)
    #   wait + 2*cap + overhead <= budget   (fit inside the job)
    # Substituting gives 2*cap < 80 - 2*cap, i.e. cap < 20 for a 90 minute
    # budget. Beyond that, fitting the job wins: a wait the runner kills
    # mid-queue looks like a failed review, while a wait shorter than the TTL
    # only means a job can give up while a DEAD holder's lease is still
    # ticking -- rare, and it degrades to a Claude review rather than a red one.
    room = JOB_BUDGET_MINUTES - (2 * cap) - JOB_OVERHEAD_MINUTES
    return ttl, max(0.0, min(wait, room))


def credential_checkout(
    store: str,
    holder: str,
    run_url: str,
    codex_home: str,
    wait_minutes: float,
    poll_seconds: float,
    token: str,
    repo: str = "",
    pr_number: int | None = None,
    target_token: str = "",
) -> int:
    """Take the lease and write the borrowed credential. Never fails the review.

    Every failure here reports `fetched=false` and exits 0. A job that cannot
    borrow the credential must review with Claude alone, not go red: a missing
    backend is already an ordinary outcome for `backends.run()`.

    The whole body runs under one broad `except Exception` backstop, below the
    specific `except GitHubError` branches. Nothing here may propagate: a
    write failure after the lease is taken, a store reply that is valid JSON
    but not an object, or a transport error that is not a `GitHubError` (a raw
    `ConnectionError`/`Timeout` from `requests`) must all still release the
    lease if held, report `fetched=false`, and exit 0.
    """
    import datetime as _dt
    from pathlib import Path

    from reviewbot import credentials

    api = _store_api(store, token)
    held = False
    try:
        # Inside the backstop on purpose: this probe talks to the network and
        # must not be the one thing in this command that can still propagate.
        policy = (
            _target_policy(repo, pr_number, target_token or token) if repo and pr_number else None
        )
        if repo and pr_number and not _will_run_codex(policy):
            print(f"reviewbot: {repo} will not reach the codex backend; not taking the lease")
            _say_output("fetched", "false")
            return 0
        ttl, sized_wait = _lease_minutes(policy)
        if wait_minutes < 0:
            wait_minutes = sized_wait
        # WAIT for the credential rather than giving up on it. One plan is
        # shared by every repository, so a review that arrives while another
        # holds the lease must queue, not silently drop the backend -- on a
        # codex-only repository giving up means no review at all.
        #
        # The wait is bounded by the lease's own TTL, not by a guess: a holder
        # that died without checking in releases it when the TTL expires, so
        # waiting longer than one TTL plus a margin can only be waiting on a
        # holder that is genuinely working.
        deadline = _dt.datetime.now(_dt.UTC) + _dt.timedelta(minutes=max(0.0, wait_minutes))
        waited = 0
        while True:
            try:
                if credentials.acquire(api, holder, run_url, _dt.datetime.now(_dt.UTC), ttl):
                    held = True
                    if waited:
                        print(f"reviewbot: took the credential lease after {waited}s")
                    break
            except GitHubError as exc:
                print(f"reviewbot: cannot reach the credential store: {scrub(str(exc))}")
                _say_output("fetched", "false")
                return 0
            now = _dt.datetime.now(_dt.UTC)
            if now >= deadline:
                print(
                    f"reviewbot: the credential lease was still held after "
                    f"{wait_minutes:g} minutes; giving up"
                )
                _say_output("fetched", "false")
                return 0
            left = (deadline - now).total_seconds()
            nap = min(poll_seconds, max(1.0, left))
            print(f"reviewbot: the credential lease is held; waiting {nap:.0f}s ({left:.0f}s left)")
            _time.sleep(nap)
            waited += int(nap)

        try:
            text = api.file_at_ref(credentials.ISSUE_PATH, credentials.ISSUE_BRANCH)
        except GitHubError as exc:
            print(f"reviewbot: cannot read the issued credential: {scrub(str(exc))}")
            credentials.release(api, holder)
            _say_output("fetched", "false")
            return 0
        # META_PATH exists so this question can be asked before the credential
        # is written: an expired copy authenticates nothing, and borrowing it
        # spends a lease and a review slot to reach a certain failure.
        try:
            meta_text = api.file_at_ref(credentials.META_PATH, credentials.ISSUE_BRANCH)
            expires = json.loads(meta_text or "{}").get("expires_at")
            if expires:
                left = (
                    _dt.datetime.fromisoformat(expires) - _dt.datetime.now(_dt.UTC)
                ).total_seconds() / 60
                if left <= 0:
                    print(
                        f"reviewbot: the issued credential expired at {expires}; not borrowing it"
                    )
                    credentials.release(api, holder)
                    _say_output("fetched", "false")
                    return 0
                print(f"reviewbot: the issued credential has {left:.0f} minutes left")
        except (GitHubError, ValueError, TypeError) as exc:
            print(f"reviewbot: could not read the credential's expiry: {scrub(str(exc))}")

        if not text:
            print("reviewbot: the store holds no issued credential")
            credentials.release(api, holder)
            _say_output("fetched", "false")
            return 0

        # Mask before writing: a later step that echoes the file must not leak
        # it. `text` may be valid JSON that is not an object (e.g. "5" or
        # "null"), so guard with isinstance rather than trust `.get` to exist.
        try:
            body = json.loads(text)
            tokens = body.get("tokens") if isinstance(body, dict) else None
            for value in (tokens or {}).values():
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
    except Exception as exc:
        print(
            f"reviewbot: credential-checkout failed unexpectedly: "
            f"{type(exc).__name__}: {scrub(str(exc))}"
        )
        if held:
            try:
                credentials.release(api, holder)
            except Exception as release_exc:
                print(
                    f"reviewbot: could not free the lease, it will expire: "
                    f"{type(release_exc).__name__}: {scrub(str(release_exc))}"
                )
        _say_output("fetched", "false")
        return 0


def _positive_days(text: str) -> int:
    """A window of zero or fewer days is refused rather than reported on.

    `--days 0` divided by a one-second window and printed a share in the
    millions; a negative value asked GitHub for commits from the future and
    printed an empty week. Both look like answers.
    """
    import argparse as _argparse

    try:
        value = int(text)
    except ValueError:
        raise _argparse.ArgumentTypeError(f"{text!r} is not a whole number of days") from None
    if value <= 0:
        raise _argparse.ArgumentTypeError("--days must be at least 1")
    return value


def lease_report(store: str, days: int, token: str) -> int:
    """Print who held the shared Codex credential, and for how long.

    UNLIKE the credential commands, this one is allowed to fail. It reviews
    nothing: a person runs it to answer "is the shared plan the bottleneck?",
    and a report that silently prints an empty week would answer it wrongly.

    It reads the store's commit history, which records every HOLD. It cannot
    count a run that WAITED -- a blocked job writes no commit -- and the report
    it prints says so rather than leaving a reader to assume otherwise.
    """
    import datetime as _dt

    from reviewbot import credentials

    now = _dt.datetime.now(_dt.UTC)
    try:
        # MARGIN, not the window itself: a hold that began before the window
        # and ended inside it arrives as a lone `lease freed` that pairs with
        # nothing, so both the hold and its in-window time disappear.
        # `credentials.lease_report` clips what this returns back to the window.
        history = _store_api(store, token).commits(
            credentials.LEASE_PATH,
            credentials.LEASE_BRANCH,
            now - _dt.timedelta(days=days) - credentials.HISTORY_MARGIN,
        )
    except GitHubError as exc:
        print(f"reviewbot lease-report: cannot read {store}: {scrub(str(exc))}")
        return 1
    print(credentials.lease_report(credentials.lease_spans(history), store, days, now))
    return 0


def credential_checkin(store: str, holder: str, codex_home: str, token: str) -> int:
    """Delete the borrowed credential, then free the lease. Never fails.

    In that order, deliberately. The lease expires by itself after its TTL; a
    credential left behind on a persistent self-hosted runner does not, so the
    delete happens before anything that could fail, including a transport
    error that is not a `GitHubError` (a raw `ConnectionError`/`Timeout` from
    `requests`).
    """
    import shutil as _shutil
    from pathlib import Path

    from reviewbot import credentials

    _shutil.rmtree(Path(codex_home), ignore_errors=True)
    try:
        credentials.release(_store_api(store, token), holder)
    except GitHubError as exc:
        print(f"reviewbot: could not free the lease, it will expire: {scrub(str(exc))}")
    except Exception as exc:
        print(
            f"reviewbot: could not free the lease, it will expire: "
            f"{type(exc).__name__}: {scrub(str(exc))}"
        )
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
    runner.add_argument(
        "--force",
        action="store_true",
        default=os.environ.get("REVIEWBOT_FORCE", "") == "true",
        help="review even a commit that has already been reviewed",
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
    checkout.add_argument(
        "--wait-minutes",
        type=float,
        default=-1.0,
        help="how long to WAIT for the shared lease before giving up. The default, -1, "
        "sizes it from the target policy's timeout_minutes so the wait always "
        "outlasts the lease TTL and a dead holder cannot make this give up early.",
    )
    checkout.add_argument(
        "--poll-seconds",
        type=float,
        default=20.0,
        help="how often to retry the lease while waiting",
    )
    checkout.add_argument(
        "--repo",
        dest="target_repo",
        default="",
        help="owner/name whose policy decides if codex runs",
    )
    checkout.add_argument(
        "--pr", dest="target_pr", type=int, default=None, help="pull request number, with --repo"
    )

    checkin = sub.add_parser("credential-checkin", help="return the Codex credential")
    checkin.add_argument("--store", required=True, help="owner/name of the credential store")
    checkin.add_argument("--holder", required=True, help="who took the lease")
    checkin.add_argument("--codex-home", required=True, help="the directory to remove")

    report = sub.add_parser("lease-report", help="who held the Codex credential, and for how long")
    report.add_argument(
        "--store",
        default="MarketData-App/code-review-credentials",
        help="owner/name of the credential store",
    )
    report.add_argument(
        "--days",
        type=_positive_days,
        default=7,
        help="how far back to read, in whole days (default 7)",
    )
    args = parser.parse_args(argv)

    if args.command == "derive-credential":
        return derive_credential(args.codex_home, args.out, args.min_hours)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("REVIEWBOT_TOKEN")
    if not token:
        parser.error("GITHUB_TOKEN is not set")

    if args.command == "credential-checkout":
        return credential_checkout(
            args.store,
            args.holder,
            args.run_url,
            args.codex_home,
            args.wait_minutes,
            args.poll_seconds,
            token,
            args.target_repo,
            args.target_pr,
            os.environ.get("REVIEWBOT_TARGET_TOKEN", ""),
        )
    if args.command == "credential-checkin":
        return credential_checkin(args.store, args.holder, args.codex_home, token)
    if args.command == "lease-report":
        return lease_report(args.store, args.days, token)

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
            force=args.force,
        )
    except Exception:  # the job must fail loudly, never silently
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
