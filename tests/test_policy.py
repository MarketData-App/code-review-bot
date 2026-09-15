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


def test_missing_proof_blocks(base_policy):
    d = policy.decide(make_result("ready", proof="missing"), make_pr(), base_policy, {})
    assert d.verdict == "blocked"
    assert "review: needs proof" in d.labels_add


def test_insufficient_proof_blocks(base_policy):
    d = policy.decide(make_result("ready", proof="insufficient"), make_pr(), base_policy, {})
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


def test_proof_paths_still_require_proof_inside_them(base_policy):
    base_policy["proof"]["paths"] = ["sdk/**"]
    d = policy.decide(make_result("ready", proof="missing"), make_pr(), base_policy, {})
    assert d.verdict == "blocked"


def test_the_proof_waived_label_lifts_the_gate(base_policy):
    pr = make_pr(labels=[policy.PROOF_WAIVED_LABEL])
    d = policy.decide(make_result("ready", proof="missing"), pr, base_policy, {})
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
