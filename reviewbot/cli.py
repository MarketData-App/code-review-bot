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
from reviewbot import config, findings, merge, policy as policy_mod, render, result as result_mod
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
        if policy_mod.wants_rereview(body):
            return int(issue["number"])
        return None
    inputs = event.get("inputs") or {}
    if inputs.get("pr"):
        return int(inputs["pr"])
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
    *, event: dict, repo: str, token: str, checkout: str, pr_number: int | None = None, api=None
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
        pr = api.gather(number, policy["max_diff_kb"], check_name)
        head_sha = pr.head_sha
    except GitHubError as exc:
        return _fail(api, "", check_name, f"could not read the pull request: {exc}")

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
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("REVIEWBOT_TOKEN")
    if not token:
        parser.error("GITHUB_TOKEN is not set")
    if not args.repo:
        parser.error("GITHUB_REPOSITORY is not set and --repo was not given")

    event = {}
    if args.event and os.path.exists(args.event):
        with open(args.event) as handle:
            event = json.load(handle)

    try:
        return run(
            event=event, repo=args.repo, token=token, checkout=args.checkout, pr_number=args.pr
        )
    except Exception:  # the job must fail loudly, never silently
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
