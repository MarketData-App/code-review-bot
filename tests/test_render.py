"""Tests for the review comment.

One comment per PR, edited in place. Disabled sections do not render, and
nothing token-shaped ever reaches it (spec sections 6 and 8).

Run: pytest tests/test_render.py
"""

import pytest

from reviewbot import config, findings, policy, render
from reviewbot.facts import PRFacts

META = {
    "repo": "MarketData-App/api",
    "reviewed_sha": "abc1234def5678901234567890123456789abcde",
    "revision": 2,
    "backends": ["claude"],
    "models": {"claude": "claude-opus-5"},
    "missing_backends": [],
    "unseen_files": [],
}


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
        diff="",
        unseen_files=[],
        ci_state="success",
        previous_comment=None,
        previous_state={},
        comments_since=[],
    )
    base.update(over)
    return PRFacts(**base)


def make_result(**over):
    items = findings.with_ids(
        [
            {
                "file": "sdk/client.py",
                "line_start": 42,
                "line_end": 44,
                "category": "correctness",
                "severity": "blocking",
                "confidence": 0.9,
                "title": "Retry loop never sleeps",
                "body": "The backoff is computed and discarded.",
                "evidence": "sdk/client.py:43",
            },
            {
                "file": None,
                "line_start": None,
                "line_end": None,
                "category": "docs",
                "severity": "nit",
                "confidence": 0.4,
                "title": "Changelog entry missing",
                "body": "Add one line.",
            },
        ]
    )
    result = {
        "summary": "Adds a retry to the candles fetch.",
        "findings": items,
        "proof": {"status": "missing", "ask": "Show the retry firing against a 503."},
        "verdict": {"value": "needs_changes", "reason": "One blocking finding."},
        "rating": {"patch": 3, "proof": 2},
    }
    result.update(over)
    return result


@pytest.fixture
def parts():
    pol = config.defaults()
    result = make_result()
    decisions = policy.decide(result, make_pr(), pol, {})
    since = findings.since_last_review([], result["findings"])
    return result, META, pol, decisions, since, {}


def test_the_comment_carries_the_marker(parts):
    from reviewbot import markers

    body = render.render(*parts)
    assert markers.MARKER in body
    assert markers.parse(body)["revision"] == 2


def test_the_verdict_headline_is_first(parts):
    body = render.render(*parts)
    assert body.lstrip().splitlines()[0].startswith("##")
    # Missing proof WARNS by default now, so the model's own verdict stands
    # rather than being overwritten. What matters is that it is not Blocked.
    assert "Blocked" not in body.splitlines()[0]


def gated_parts():
    """The same fixture with the proof gate opted in, which repos may do."""
    pol = config.defaults()
    pol["proof"]["required"] = True
    result = make_result()
    decisions = policy.decide(result, make_pr(), pol, {})
    since = findings.since_last_review([], result["findings"])
    return result, META, pol, decisions, since, {}


def test_a_repository_that_opts_into_the_proof_gate_still_renders_blocked():
    body = render.render(*gated_parts())
    assert "Blocked" in body.splitlines()[0]
    assert "runtime evidence is missing" in body


def test_the_summary_is_present(parts):
    assert "Adds a retry to the candles fetch." in render.render(*parts)


def test_the_reasons_are_listed():
    # The reasons list is what carries a gate's explanation, so assert it on a
    # policy that actually has one enabled.
    assert "runtime evidence is missing" in render.render(*gated_parts())


def test_the_rating_row_renders_when_enabled(parts):
    body = render.render(*parts)
    assert "patch 3/6" in body and "proof 2/6" in body and "overall 2/6" in body


def test_the_rating_row_names_the_tiers_in_the_schema_words(parts):
    body = render.render(*parts)
    assert "incomplete" in body  # patch tier 3
    assert "claimed" in body  # proof tier 2


def test_the_rating_row_is_absent_when_disabled(parts):
    result, meta, pol, decisions, since, waived = parts
    pol["ratings"] = False
    assert "patch 3/6" not in render.render(result, meta, pol, decisions, since, waived)


def test_before_merge_lists_blocking_findings_and_the_proof_ask(parts):
    body = render.render(*parts)
    assert "### Before merge" in body
    assert "Retry loop never sleeps" in body
    assert "Show the retry firing against a 503." in body


def test_findings_group_by_severity(parts):
    body = render.render(*parts)
    assert "**Blocking**" in body
    assert "**Nit**" in body


def test_a_located_finding_links_to_the_file_and_line(parts):
    body = render.render(*parts)
    assert "blob/abc1234def5678901234567890123456789abcde/sdk/client.py#L42" in body


def test_an_unlocated_finding_renders_without_a_link(parts):
    body = render.render(*parts)
    assert "Changelog entry missing" in body


def test_the_finding_id_is_shown_so_a_maintainer_can_waive_it(parts):
    result = parts[0]
    assert result["findings"][0]["id"] in render.render(*parts)


def test_a_decision_packet_renders(parts):
    result, meta, pol, _, since, waived = parts
    result["decision"] = {
        "question": "Break the response shape?",
        "options": ["Keep it", "Break it"],
        "recommendation": "Keep it",
    }
    decisions = policy.decide(result, make_pr(), pol, waived)
    body = render.render(result, meta, pol, decisions, since, waived)
    assert "### Decision needed" in body
    assert "Break the response shape?" in body
    assert "Keep it" in body


def test_a_decision_packet_is_hidden_when_disabled(parts):
    result, meta, pol, decisions, since, waived = parts
    pol["decision_packets"] = False
    result["decision"] = {"question": "q", "options": ["a", "b"], "recommendation": "a"}
    assert "### Decision needed" not in render.render(result, meta, pol, decisions, since, waived)


def test_since_last_review_reports_the_three_groups(parts):
    result, meta, pol, decisions, _, waived = parts
    since = {"resolved": ["deadbeef"], "still_open": [result["findings"][0]], "new": []}
    body = render.render(result, meta, pol, decisions, since, waived)
    assert "### Since last review" in body
    assert "deadbeef" in body
    assert "1 still open" in body


def test_the_first_review_has_no_since_section(parts):
    result, meta, pol, decisions, _, waived = parts
    since = {"resolved": [], "still_open": [], "new": result["findings"]}
    assert "### Since last review" not in render.render(result, meta, pol, decisions, since, waived)


def test_no_praise_section_is_rendered(parts):
    assert "### Praise" not in render.render(*parts)


def test_a_waived_finding_is_marked_and_not_in_before_merge(parts):
    result, meta, pol, _, since, _ = parts
    waived = {result["findings"][0]["id"]: "selden"}
    decisions = policy.decide(result, make_pr(), pol, waived)
    body = render.render(result, meta, pol, decisions, since, waived)
    assert "waived by selden" in body
    before = body.split("### Findings")[0]
    assert "Retry loop never sleeps" not in before


def test_the_footer_names_the_backend_the_model_the_sha_and_the_revision(parts):
    body = render.render(*parts)
    footer = body.strip().splitlines()[-3]
    assert "claude" in footer and "claude-opus-5" in footer
    assert "abc1234" in footer and "revision 2" in footer


def test_a_missing_backend_is_named_in_the_footer(parts):
    result, meta, pol, decisions, since, waived = parts
    meta = dict(meta, missing_backends=["codex"])
    assert "codex unavailable" in render.render(result, meta, pol, decisions, since, waived)


def test_unseen_files_are_named(parts):
    result, meta, pol, decisions, since, waived = parts
    meta = dict(meta, unseen_files=["big/generated.py"])
    body = render.render(result, meta, pol, decisions, since, waived)
    assert "big/generated.py" in body
    assert "not read" in body.lower()


def test_backend_tags_appear_only_when_two_backends_ran(parts):
    result, meta, pol, decisions, since, waived = parts
    assert "`claude`)" not in render.render(result, meta, pol, decisions, since, waived)
    result["findings"][0]["backends"] = ["claude", "codex"]
    result["findings"][0]["agreed"] = True
    two = dict(
        meta,
        backends=["claude", "codex"],
        models={"claude": "claude-opus-5", "codex": "gpt-5.6-sol"},
    )
    assert "both" in render.render(result, two, pol, decisions, since, waived)


def test_one_reviewer_noted_section_when_agreement_is_required(parts):
    result, meta, pol, decisions, since, waived = parts
    pol["require_agreement"] = True
    result["findings"][0]["backends"] = ["claude"]
    result["findings"][0]["agreed"] = False
    two = dict(
        meta,
        backends=["claude", "codex"],
        models={"claude": "claude-opus-5", "codex": "gpt-5.6-sol"},
    )
    body = render.render(result, two, pol, decisions, since, waived)
    assert "### One reviewer noted" in body


def test_no_token_shaped_string_reaches_the_comment(parts):
    # The renderer sees no token, and must not learn to pass one through.
    result, meta, pol, decisions, since, waived = parts
    result["summary"] = "ghs_" + "A" * 36
    body = render.render(result, meta, pol, decisions, since, waived)
    assert "ghs_" not in body
    assert "[redacted]" in body


def test_check_title_is_short(parts):
    title = render.check_title(parts[3])
    assert len(title) <= 60
