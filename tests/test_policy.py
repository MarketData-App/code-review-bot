"""Tests for the pure decisions.

The model reports. The engine decides. Nothing here calls out, so every rule
in spec sections 5 and 8 is pinned by a test that runs in milliseconds.

Run: pytest tests/test_policy.py
"""

import pytest

from reviewbot import config, findings, policy
from reviewbot.facts import PRFacts


def make_pr(**over):
    base = dict(
        number=7,
        title="Add a retry",
        body="",
        author="alice",
        author_is_bot=False,
        draft=False,
        labels=[],
        head_sha="a" * 40,
        base_ref="main",
        node_id="PR_1",
        author_association="MEMBER",
        changed_files=[
            {"path": "sdk/client.py", "status": "modified", "additions": 3, "deletions": 1}
        ],
        diff="diff",
        unseen_files=[],
        ci_state="success",
        previous_comment=None,
        previous_state={},
        comments_since=[],
    )
    base.update(over)
    return PRFacts(**base)


def make_result(verdict="ready", proof="sufficient", severities=(), decision=None):
    items = [
        {
            "file": "sdk/client.py",
            "line_start": 3,
            "line_end": None,
            "category": "correctness",
            "severity": s,
            "confidence": 0.9,
            "title": f"Finding {i}",
            "body": "text",
        }
        for i, s in enumerate(severities)
    ]
    result = {
        "summary": "s",
        "findings": findings.with_ids(items),
        "proof": {"status": proof, "ask": "Show it running."},
        "verdict": {"value": verdict, "reason": "r"},
        "rating": {"patch": 5, "proof": 5},
    }
    if decision:
        result["decision"] = decision
    return result


@pytest.fixture
def base_policy():
    return config.defaults()


@pytest.fixture
def proof_gated():
    """`base_policy` with the proof gate ON.

    The gate warns by default now -- it used to overwrite the verdict for every
    pull request, which reported a 6/6 review with no findings as Blocked. The
    tests below are about the gate itself, so they opt in explicitly.
    """
    pol = config.defaults()
    pol["proof"]["required"] = True
    return pol


# --- skips -----------------------------------------------------------------


def test_no_skip_for_an_ordinary_pr(base_policy):
    assert policy.should_skip(make_pr(), base_policy) is None


def test_draft_is_skipped_by_default(base_policy):
    assert "draft" in policy.should_skip(make_pr(draft=True), base_policy)


def test_draft_is_reviewed_when_policy_says_so(base_policy):
    base_policy["review_drafts"] = True
    assert policy.should_skip(make_pr(draft=True), base_policy) is None


def test_ignored_author_is_skipped(base_policy):
    assert "dependabot[bot]" in policy.should_skip(make_pr(author="dependabot[bot]"), base_policy)


def test_skip_review_in_the_title_is_honoured(base_policy):
    assert "[skip review]" in policy.should_skip(
        make_pr(title="Bump deps [skip review]"), base_policy
    )


def test_all_files_ignored_is_skipped(base_policy):
    pr = make_pr(
        changed_files=[{"path": "uv.lock", "status": "modified", "additions": 9, "deletions": 9}]
    )
    assert "ignore_paths" in policy.should_skip(pr, base_policy)


def test_one_reviewable_file_is_enough(base_policy):
    pr = make_pr(
        changed_files=[
            {"path": "uv.lock", "status": "modified", "additions": 9, "deletions": 9},
            {"path": "sdk/client.py", "status": "modified", "additions": 1, "deletions": 0},
        ]
    )
    assert policy.should_skip(pr, base_policy) is None


def test_a_pr_with_no_files_is_skipped(base_policy):
    assert policy.should_skip(make_pr(changed_files=[]), base_policy) is not None


# --- verdict and conclusion ------------------------------------------------


def test_ready_is_success(base_policy):
    d = policy.decide(make_result("ready"), make_pr(), base_policy, {})
    assert (d.verdict, d.conclusion) == ("ready", "success")


def test_needs_changes_is_neutral(base_policy):
    d = policy.decide(
        make_result("needs_changes", severities=("should_fix",)), make_pr(), base_policy, {}
    )
    assert d.conclusion == "neutral"


def test_blocked_with_the_gate_on_is_failure(base_policy):
    d = policy.decide(make_result("blocked", severities=("blocking",)), make_pr(), base_policy, {})
    assert d.conclusion == "failure"


def test_blocked_with_the_gate_off_is_neutral(base_policy):
    base_policy["gate"] = False
    d = policy.decide(make_result("blocked", severities=("blocking",)), make_pr(), base_policy, {})
    assert (d.verdict, d.conclusion) == ("blocked", "neutral")


def test_a_blocking_finding_stops_ready(base_policy):
    d = policy.decide(make_result("ready", severities=("blocking",)), make_pr(), base_policy, {})
    assert d.verdict == "needs_changes"
    assert any("blocking" in r for r in d.reasons)


def test_a_nit_does_not_stop_ready(base_policy):
    d = policy.decide(make_result("ready", severities=("nit",)), make_pr(), base_policy, {})
    assert d.verdict == "ready"


def test_a_waived_blocking_finding_stops_nothing(base_policy):
    result = make_result("ready", severities=("blocking",))
    waived = {result["findings"][0]["id"]: "selden"}
    d = policy.decide(result, make_pr(), base_policy, waived)
    assert d.verdict == "ready"


def test_require_agreement_ignores_a_single_reviewer_finding(base_policy):
    base_policy["require_agreement"] = True
    result = make_result("ready", severities=("blocking",))
    result["findings"][0]["agreed"] = False
    d = policy.decide(result, make_pr(), base_policy, {})
    assert d.verdict == "ready"


def test_require_agreement_still_honours_an_agreed_finding(base_policy):
    base_policy["require_agreement"] = True
    result = make_result("ready", severities=("blocking",))
    result["findings"][0]["agreed"] = True
    d = policy.decide(result, make_pr(), base_policy, {})
    assert d.verdict == "needs_changes"


# --- the proof gate --------------------------------------------------------


def test_missing_proof_blocks(proof_gated):
    d = policy.decide(make_result("ready", proof="missing"), make_pr(), proof_gated, {})
    assert d.verdict == "blocked"
    assert "review: needs proof" in d.labels_add


def test_insufficient_proof_blocks(proof_gated):
    d = policy.decide(make_result("ready", proof="insufficient"), make_pr(), proof_gated, {})
    assert d.verdict == "blocked"


def test_not_applicable_proof_does_not_block(base_policy):
    d = policy.decide(make_result("ready", proof="not_applicable"), make_pr(), base_policy, {})
    assert d.verdict == "ready"


def test_proof_is_not_required_when_policy_turns_it_off(base_policy):
    base_policy["proof"]["required"] = False
    d = policy.decide(make_result("ready", proof="missing"), make_pr(), base_policy, {})
    assert d.verdict == "ready"


def test_proof_paths_limit_where_proof_is_required(base_policy):
    base_policy["proof"]["paths"] = ["server/**"]
    d = policy.decide(make_result("ready", proof="missing"), make_pr(), base_policy, {})
    assert d.verdict == "ready"


def test_proof_paths_still_require_proof_inside_them(proof_gated):
    proof_gated["proof"]["paths"] = ["sdk/**"]
    d = policy.decide(make_result("ready", proof="missing"), make_pr(), proof_gated, {})
    assert d.verdict == "blocked"


def test_the_proof_waived_label_lifts_the_gate(proof_gated):
    pr = make_pr(labels=[policy.PROOF_WAIVED_LABEL])
    d = policy.decide(make_result("ready", proof="missing"), pr, proof_gated, {})
    assert d.verdict == "ready"
    assert any("waived" in r for r in d.reasons)


# --- CI and unseen files ---------------------------------------------------


def test_red_ci_blocks(base_policy):
    d = policy.decide(make_result("ready"), make_pr(ci_state="failure"), base_policy, {})
    assert d.verdict == "blocked"
    assert any("CI" in r for r in d.reasons)


def test_pending_ci_does_not_block(base_policy):
    d = policy.decide(make_result("ready"), make_pr(ci_state="pending"), base_policy, {})
    assert d.verdict == "ready"


def test_red_ci_is_ignored_when_policy_says_so(base_policy):
    base_policy["require_ci_green"] = False
    d = policy.decide(make_result("ready"), make_pr(ci_state="failure"), base_policy, {})
    assert d.verdict == "ready"


def test_unseen_files_stop_ready(base_policy):
    d = policy.decide(make_result("ready"), make_pr(unseen_files=["big.py"]), base_policy, {})
    assert d.verdict == "needs_changes"
    assert any("big.py" in r for r in d.reasons)


def test_unseen_files_may_be_allowed(base_policy):
    base_policy["allow_ready_with_unseen_files"] = True
    d = policy.decide(make_result("ready"), make_pr(unseen_files=["big.py"]), base_policy, {})
    assert d.verdict == "ready"


# --- labels ----------------------------------------------------------------


def test_ready_sets_one_label_and_clears_the_others(base_policy):
    d = policy.decide(make_result("ready"), make_pr(), base_policy, {})
    assert d.labels_add == ["review: ready"]
    assert set(d.labels_remove) == set(policy.OWNED_LABELS) - {"review: ready"}


def test_a_decision_packet_adds_its_label(base_policy):
    result = make_result(
        "needs_changes",
        decision={
            "question": "Break the response shape?",
            "options": ["a", "b"],
            "recommendation": "a",
        },
    )
    d = policy.decide(result, make_pr(), base_policy, {})
    assert "review: decision needed" in d.labels_add


def test_decision_packets_can_be_switched_off(base_policy):
    base_policy["decision_packets"] = False
    result = make_result(
        "needs_changes", decision={"question": "q", "options": ["a", "b"], "recommendation": "a"}
    )
    d = policy.decide(result, make_pr(), base_policy, {})
    assert "review: decision needed" not in d.labels_add


# --- approval --------------------------------------------------------------


def test_no_approval_by_default(base_policy):
    assert policy.decide(make_result("ready"), make_pr(), base_policy, {}).approve is False


def test_approval_when_enabled_and_ready(base_policy):
    base_policy["auto_approve"] = True
    assert policy.decide(make_result("ready"), make_pr(), base_policy, {}).approve is True


def test_no_approval_when_not_ready(base_policy):
    base_policy["auto_approve"] = True
    d = policy.decide(
        make_result("needs_changes", severities=("should_fix",)), make_pr(), base_policy, {}
    )
    assert d.approve is False


def test_approval_paths_restrict_approval(base_policy):
    base_policy["auto_approve"] = True
    base_policy["auto_approve_paths"] = ["docs/**"]
    assert policy.decide(make_result("ready"), make_pr(), base_policy, {}).approve is False


def test_approval_paths_allow_a_matching_pr(base_policy):
    base_policy["auto_approve"] = True
    base_policy["auto_approve_paths"] = ["sdk/**"]
    assert policy.decide(make_result("ready"), make_pr(), base_policy, {}).approve is True


# --- auto-merge ------------------------------------------------------------


def test_auto_merge_is_left_alone_when_disabled(base_policy):
    assert policy.decide(make_result("ready"), make_pr(), base_policy, {}).auto_merge == "leave"


def enable_merge(base_policy):
    base_policy["auto_merge"]["enabled"] = True
    base_policy["auto_merge"]["authors"] = ["alice"]
    return base_policy


def test_auto_merge_arms_when_every_condition_holds(base_policy):
    d = policy.decide(make_result("ready"), make_pr(), enable_merge(base_policy), {})
    assert d.auto_merge == "arm"


def test_auto_merge_disarms_for_an_author_off_the_list(base_policy):
    enable_merge(base_policy)
    d = policy.decide(make_result("ready"), make_pr(author="mallory"), base_policy, {})
    assert d.auto_merge == "disarm"
    assert any("author" in r for r in d.reasons)


def test_auto_merge_disarms_on_pending_ci(base_policy):
    enable_merge(base_policy)
    d = policy.decide(make_result("ready"), make_pr(ci_state="pending"), base_policy, {})
    assert d.auto_merge == "disarm"


def test_auto_merge_disarms_on_an_open_decision(base_policy):
    enable_merge(base_policy)
    result = make_result(
        "ready", decision={"question": "q", "options": ["a", "b"], "recommendation": "a"}
    )
    d = policy.decide(result, make_pr(), base_policy, {})
    assert d.auto_merge == "disarm"


def test_auto_merge_disarms_when_not_ready(base_policy):
    enable_merge(base_policy)
    d = policy.decide(make_result("blocked", severities=("blocking",)), make_pr(), base_policy, {})
    assert d.auto_merge == "disarm"


# --- comment commands ------------------------------------------------------


def test_rereview_command_is_recognised():
    assert policy.wants_rereview(f"{policy.HANDLE} re-review")
    assert policy.wants_rereview(f"  {policy.HANDLE}   RE-REVIEW  please")


def test_an_unrelated_comment_is_not_a_rereview():
    assert not policy.wants_rereview("please re-review this")
    assert not policy.wants_rereview("")


def test_a_maintainer_waiver_is_collected():
    comments = [
        {"author": "selden", "body": f"{policy.HANDLE} waive 1234abcd", "is_maintainer": True}
    ]
    assert policy.collect_waivers(comments, {}) == {"1234abcd": "selden"}


def test_a_non_maintainer_waiver_is_ignored():
    comments = [
        {"author": "alice", "body": f"{policy.HANDLE} waive 1234abcd", "is_maintainer": False}
    ]
    assert policy.collect_waivers(comments, {}) == {}


def test_previous_waivers_are_carried_forward():
    assert policy.collect_waivers([], {"aaaabbbb": "selden"}) == {"aaaabbbb": "selden"}


def test_several_waivers_in_one_comment():
    comments = [
        {
            "author": "selden",
            "body": f"{policy.HANDLE} waive 1234abcd 5678efab",
            "is_maintainer": True,
        }
    ]
    assert set(policy.collect_waivers(comments, {})) == {"1234abcd", "5678efab"}


# --- the org-only gate -----------------------------------------------------
#
# The bot reviews pull requests from the organisation only. This is the
# security boundary, not a preference: a model on the self-hosted runner can
# read any file on the machine (spec section 6), so an outsider's pull request
# must never reach it. The workflow refuses first; this is the backstop for a
# caller workflow that is misconfigured.


def test_an_org_member_is_reviewed(base_policy):
    assert policy.should_skip(make_pr(author_association="MEMBER"), base_policy) is None


def test_the_repository_owner_is_reviewed(base_policy):
    assert policy.should_skip(make_pr(author_association="OWNER"), base_policy) is None


def test_an_outside_contributor_is_refused(base_policy):
    assert policy.is_trusted("mallory", "CONTRIBUTOR", base_policy) is False


def test_a_first_time_contributor_is_refused(base_policy):
    assert policy.is_trusted("new", "FIRST_TIME_CONTRIBUTOR", base_policy) is False


def test_an_unknown_association_is_refused(base_policy):
    assert policy.is_trusted("x", "NONE", base_policy) is False
    assert policy.is_trusted("x", "", base_policy) is False


def test_a_listed_bot_is_reviewed_despite_its_association(base_policy):
    # A bot opening a pull request has association NONE or CONTRIBUTOR even
    # when the bot is ours, so agent pull requests need the explicit list.
    base_policy["trusted_authors"] = ["sdk-bot[bot]"]
    pr = make_pr(author="sdk-bot[bot]", author_association="NONE", author_is_bot=True)
    assert policy.should_skip(pr, base_policy) is None


def test_an_unlisted_bot_is_refused(base_policy):
    assert policy.is_trusted("stranger-bot[bot]", "NONE", base_policy) is False


def test_ignore_authors_still_wins_over_the_allow_list(base_policy):
    base_policy["trusted_authors"] = ["dependabot[bot]"]
    pr = make_pr(author="dependabot[bot]", author_association="NONE")
    assert "ignore_authors" in policy.should_skip(pr, base_policy)


def test_the_trusted_associations_are_configurable(base_policy):
    base_policy["trusted_associations"] = ["OWNER"]
    assert policy.is_trusted("a", "MEMBER", base_policy) is False
    assert policy.is_trusted("a", "OWNER", base_policy) is True


def test_is_trusted_is_usable_on_its_own(base_policy):
    assert policy.is_trusted("alice", "MEMBER", base_policy) is True
    assert policy.is_trusted("mallory", "CONTRIBUTOR", base_policy) is False


def test_collaborator_is_not_an_org_member_by_default(base_policy):
    # GitHub says COLLABORATOR for anyone invited at any permission level.
    assert base_policy["trusted_associations"] == ["OWNER", "MEMBER"]
    assert policy.is_trusted("x", "COLLABORATOR", base_policy) is False


def test_a_repo_may_opt_collaborators_in(base_policy):
    base_policy["trusted_associations"] = ["OWNER", "MEMBER", "COLLABORATOR"]
    assert policy.should_skip(make_pr(author_association="COLLABORATOR"), base_policy) is None


def test_trusted_authors_tolerates_spaces_and_case(base_policy):
    base_policy["trusted_authors"] = ["sdk-sync[bot]"]
    assert policy.is_trusted("SDK-Sync[bot]", "NONE", base_policy) is True


def test_parse_author_list_strips_and_drops_empties():
    assert policy.parse_author_list("a[bot], b[bot] ,, c") == ["a[bot]", "b[bot]", "c"]
    assert policy.parse_author_list("") == []
    assert policy.parse_author_list(None) == []


def test_only_the_documented_commands_are_bot_commands():
    # Spec section 8 defines two: re-review and waive. Treating every comment
    # addressed to the bot as a trigger widened the contract silently.
    assert policy.is_bot_command(f"{policy.HANDLE} re-review")
    assert policy.is_bot_command(f"{policy.HANDLE} waive 1234abcd")
    assert policy.is_bot_command(f"  {policy.HANDLE}   WAIVE 1234abcd  ")
    assert not policy.is_bot_command(f"{policy.HANDLE} thanks, nice review")
    assert not policy.is_bot_command(f"{policy.HANDLE}")
    assert not policy.is_bot_command("re-review please")
    assert not policy.is_bot_command("")


def test_proof_paths_is_an_any_match_over_the_whole_pull_request(proof_gated):
    # Not a per-file filter. One matching file switches the gate on for the
    # whole pull request, including the files that do not match. The two path
    # options use opposite quantifiers: proof.paths is ANY, auto_approve_paths
    # is ALL.
    proof_gated["proof"]["paths"] = ["src/**"]
    pr = make_pr(
        changed_files=[
            {"path": "README.md", "status": "modified", "additions": 1, "deletions": 0},
            {"path": "src/client.py", "status": "modified", "additions": 1, "deletions": 0},
        ]
    )
    assert policy.proof_applies(pr, proof_gated) is True


def test_proof_paths_is_a_floor_not_a_filter(base_policy):
    # Its real use: a pull request that touches nothing matching can never be
    # blocked for missing proof, whatever the model decides. That is a floor
    # under the model's judgement on docs-only changes.
    base_policy["proof"]["paths"] = ["src/**"]
    pr = make_pr(
        changed_files=[
            {"path": "README.md", "status": "modified", "additions": 1, "deletions": 0},
        ]
    )
    assert policy.proof_applies(pr, base_policy) is False
    decision = policy.decide(make_result("ready", proof="missing"), pr, base_policy, {})
    assert decision.verdict == "ready"


# --- missing proof warns; it does not block --------------------------------


def test_missing_proof_does_not_block_by_default():
    """The model's verdict stands; the ask goes in the comment, not the gate.

    Measured on the first three real reviews this bot produced: sdk-py #122
    scored patch 6/6 with ZERO findings and was still reported Blocked, because
    `proof_unmet` overwrote the verdict outright. A reviewer that blocks every
    pull request regardless of quality gets ignored, which is worse than none.
    """
    pol = config.defaults()
    assert pol["proof"]["required"] is False
    out = policy.decide(make_result(proof="missing"), make_pr(), pol, {})
    assert out.verdict == "ready"
    assert out.conclusion == "success"
    assert not any("runtime evidence" in r for r in out.reasons)


def test_a_repository_can_still_opt_into_blocking_on_missing_proof():
    pol = config.load("proof:\n  required: true\n")
    out = policy.decide(make_result(proof="missing"), make_pr(), pol, {})
    assert out.verdict == "blocked"
    assert any("runtime evidence" in r for r in out.reasons)


def test_opting_in_still_honours_the_paths_floor():
    pol = config.load("proof:\n  required: true\n  paths: ['src/money/**']\n")
    untouched = make_pr(
        changed_files=[{"path": "README.md", "status": "modified", "additions": 1, "deletions": 0}]
    )
    out = policy.decide(make_result(proof="missing"), untouched, pol, {})
    assert out.verdict == "ready"


def test_blocking_findings_still_block_without_the_proof_gate():
    # Turning the proof gate down must not turn the review down.
    out = policy.decide(
        make_result(proof="missing", severities=("blocking",)), make_pr(), config.defaults(), {}
    )
    assert out.verdict != "ready"


# --- an unchanged head is not reviewed twice --------------------------------


REVIEWED = {"reviewed_sha": "a" * 40, "revision": 3, "finding_ids": ["x1"]}


def test_a_head_that_was_already_reviewed_is_skipped():
    """Re-reviewing the same commit spends tokens to rewrite the same comment.

    `same_sha` already existed but only chose the revision NUMBER -- it never
    stopped the run. That was harmless while the trigger was manual, and stops
    being harmless the moment `pull_request_target` is armed: its `edited` type
    fires on a title or description tweak, at an unchanged commit.
    """
    pr = make_pr(head_sha="a" * 40, previous_state=REVIEWED)
    assert should_skip_reason(pr) == "this commit has already been reviewed"


def test_a_new_head_is_reviewed():
    pr = make_pr(head_sha="b" * 40, previous_state=REVIEWED)
    assert should_skip_reason(pr) is None


def test_a_first_review_is_never_skipped():
    pr = make_pr(head_sha="a" * 40, previous_state={})
    assert should_skip_reason(pr) is None


def test_a_bot_command_re_reviews_an_unchanged_head():
    # Asking for it explicitly must always work, or a waiver cannot take
    # effect until the author happens to push.
    pr = make_pr(
        head_sha="a" * 40,
        previous_state=REVIEWED,
        comments_since=[{"body": "@marketdata-code-review re-review"}],
    )
    assert should_skip_reason(pr) is None


def test_a_waiver_re_reviews_an_unchanged_head():
    pr = make_pr(
        head_sha="a" * 40,
        previous_state=REVIEWED,
        comments_since=[{"body": "@marketdata-code-review waive 580fb334"}],
    )
    assert should_skip_reason(pr) is None


def test_an_unrelated_comment_does_not_re_review():
    pr = make_pr(
        head_sha="a" * 40,
        previous_state=REVIEWED,
        comments_since=[{"body": "thanks, looks good"}],
    )
    assert should_skip_reason(pr) == "this commit has already been reviewed"


def test_force_overrides_the_skip():
    pr = make_pr(head_sha="a" * 40, previous_state=REVIEWED)
    assert policy.should_skip(pr, config.defaults(), force=True) is None


def should_skip_reason(pr):
    return policy.should_skip(pr, config.defaults())


# --- do not spend a review on a pull request that is not green --------------


def test_a_red_pull_request_is_not_reviewed_at_all():
    """The model must not run. It used to run, then have its verdict flipped.

    `require_ci_green` was only consulted in `decide()`, which happens AFTER
    `backends.run()`. So a failing pull request bought a full review, tokens
    and all, and then had the result overwritten with "CI is red".
    """
    pr = make_pr(ci_state="failure")
    assert policy.should_skip(pr, config.defaults()) == "CI is red on the head commit"


def test_a_pending_pull_request_is_not_reviewed_yet():
    pr = make_pr(ci_state="pending")
    assert policy.should_skip(pr, config.defaults()) == "CI has not finished on the head commit"


def test_a_green_pull_request_is_reviewed():
    assert policy.should_skip(make_pr(ci_state="success"), config.defaults()) is None


def test_a_repository_with_no_ci_at_all_is_still_reviewed():
    # "none" is no opinion, not a failure. A repository without CI must not
    # become a repository without review.
    assert policy.should_skip(make_pr(ci_state="none"), config.defaults()) is None


def test_turning_the_ci_requirement_off_reviews_anyway():
    pol = config.load("require_ci_green: false\n")
    assert policy.should_skip(make_pr(ci_state="failure"), pol) is None


def test_force_reviews_a_red_pull_request():
    # Asking deliberately must still work -- debugging a review on a branch
    # whose CI is red is a real thing to want.
    pr = make_pr(ci_state="failure")
    assert policy.should_skip(pr, config.defaults(), force=True) is None
