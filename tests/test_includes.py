"""The include block at the top of a repository's REVIEW.md.

A repository's rules file names the shipped rule sets it wants, one per line,
before anything else:

    @include default
    @include sdk

    ## House rules for this repository

The shape this replaces read only the FIRST line, so a second `@include` was
passed to the model as literal text and did nothing. Silence was the defect:
the rules a repository asked for were simply absent, and the review came back
thin with no sign of why.

Two rules hold the whole design up.

1. An include names a rule set the BOT ships. It never names a path, so a
   pull request cannot point the loader at a file of its choosing.
2. A name the bot does not ship fails the run. `config.py` already treats an
   unknown policy key that way, and for the same reason: a typo that quietly
   drops the security rules is worse than a red check run.

Run: pytest tests/test_includes.py
"""

import pytest

from reviewbot import brief

# --- what the bot ships ---------------------------------------------------


def test_every_shipped_rule_set_resolves_and_says_something():
    names = brief.available_rule_sets()
    assert "default" in names
    assert "sdk" in names
    for name in names:
        assert len(brief.rule_set(name).strip()) > 200, name


def test_default_review_is_the_default_rule_set():
    assert brief.default_review() == brief.rule_set("default")


def test_an_unknown_rule_set_is_refused_by_name():
    with pytest.raises(brief.ReviewError) as exc:
        brief.rule_set("no-such-thing")
    assert "no-such-thing" in str(exc.value)


def test_a_rule_set_name_cannot_walk_out_of_the_rules_directory():
    """The name is a key, never a path. A traversal is an unknown name."""
    for attempt in ["../defaults/policy", "../../etc/passwd", "sdk/../default", "/etc/passwd"]:
        with pytest.raises(brief.ReviewError):
            brief.rule_set(attempt)


# --- parsing the include block -------------------------------------------


def test_no_repo_file_gives_the_default():
    assert brief.load_review(None) == brief.default_review()
    assert brief.load_review("   ") == brief.default_review()


def test_a_repo_file_with_no_include_replaces_the_default():
    text = brief.load_review("Only review the SDK surface.")
    assert text == "Only review the SDK surface."
    assert "Standing review instructions" not in text


def test_one_include_keeps_the_rule_set_and_appends_the_repo_text():
    text = brief.load_review("@include default\n\nEvery public method needs a docstring.")
    assert "Standing review instructions" in text
    assert "Every public method needs a docstring." in text
    assert "@include" not in text


def test_two_includes_both_arrive():
    text = brief.load_review("@include default\n@include sdk\n\nHouse rules.")
    assert "Standing review instructions" in text
    assert "The version bump is a calculation, not an opinion" in text
    assert "House rules." in text
    assert "@include" not in text


def test_the_rule_sets_arrive_in_the_order_they_were_written():
    forwards = brief.load_review("@include default\n@include sdk\n")
    backwards = brief.load_review("@include sdk\n@include default\n")
    assert forwards.index("Standing review instructions") < forwards.index("SDK pull request rules")
    assert backwards.index("SDK pull request rules") < backwards.index(
        "Standing review instructions"
    )


def test_the_repo_text_always_comes_last():
    text = brief.load_review("@include default\n@include sdk\n\nHOUSE.")
    assert text.rstrip().endswith("HOUSE.")


def test_blank_lines_inside_the_include_block_are_allowed():
    text = brief.load_review("@include default\n\n@include sdk\n\nHouse rules.")
    assert "Standing review instructions" in text
    assert "SDK pull request rules" in text


def test_surrounding_space_and_case_do_not_matter():
    text = brief.load_review("  @INCLUDE Default  \n\nHouse rules.")
    assert "Standing review instructions" in text
    assert "House rules." in text


def test_an_include_below_the_block_is_text_and_not_a_directive():
    """Otherwise a rules file could not quote the directive to document it."""
    text = brief.load_review("House rules.\n@include default\n")
    assert "Standing review instructions" not in text
    assert "@include default" in text


def test_an_include_block_with_no_repo_text_is_just_the_rule_sets():
    text = brief.load_review("@include default\n")
    assert text.strip() == brief.rule_set("default").strip()


def test_an_unknown_include_fails_the_run_and_names_it():
    with pytest.raises(brief.ReviewError) as exc:
        brief.load_review("@include defualt\n\nHouse rules.")
    assert "defualt" in str(exc.value)


def test_a_repeated_include_is_refused():
    """It is always a mistake, and silently ignoring it hides the mistake."""
    with pytest.raises(brief.ReviewError) as exc:
        brief.load_review("@include default\n@include default\n")
    assert "default" in str(exc.value)


def test_an_include_with_no_name_is_refused():
    with pytest.raises(brief.ReviewError):
        brief.load_review("@include\n\nHouse rules.")


# --- what the caller needs for the footer ---------------------------------


def test_resolve_reports_the_rule_sets_it_used():
    resolved = brief.resolve_review("@include default\n@include sdk\n\nHouse rules.")
    assert resolved.includes == ["default", "sdk"]
    assert "House rules." in resolved.text


def test_resolve_reports_no_rule_sets_for_a_repo_file_that_replaces_them():
    resolved = brief.resolve_review("House rules only.")
    assert resolved.includes == []


def test_resolve_reports_the_default_when_there_is_no_repo_file():
    resolved = brief.resolve_review(None)
    assert resolved.includes == ["default"]
    assert resolved.text == brief.default_review()
