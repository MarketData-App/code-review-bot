"""The `sdk` rule set: what it must keep saying, and who it must not bind.

It used to be assembled by `docs/sdk/build.sh` and copied into six repositories
as one large generated file. It now ships as `reviewbot/rules/sdk.md` and the
repositories reach it with `@include sdk`, so these tests read the shipped file
rather than a build output.

The per-language surface rules moved the other way, into each repository's own
REVIEW.md. They have exactly one consumer each, so there was no drift for a
central copy to prevent -- only a build script to maintain.

Run: pytest tests/test_sdk_rules.py
"""

import pytest

from reviewbot import brief

SDK = brief.rule_set("sdk")


def test_the_version_bump_is_posed_as_a_calculation():
    """The rule that stops a major release shipping by accident."""
    assert "The version bump is a calculation, not an opinion" in SDK
    assert "Do not form an opinion. Compare." in SDK


def test_the_two_breaking_questions_stay_apart():
    assert "Is the API change breaking?" in SDK
    assert "break THIS SDK's public surface?" in SDK


def test_the_sibling_defect_rule_survives_and_does_not_block():
    assert "probably a defect in the others" in SDK
    assert "Report this as `should_fix`, never as `blocking`." in SDK


def test_the_requirements_document_stays_authoritative():
    assert "marketdata.app/docs/internal/sdk-requirements/" in SDK


def test_the_surface_rules_are_not_here():
    """`build.sh` used to append `surface/<lang>.md` to this text.

    Nothing does now: each repository carries its own surface section. The
    shared file still CITES Python and Java as illustrations of why that part
    cannot be shared, which is the point -- what must be absent is the surface
    section itself.
    """
    assert "The public surface of this SDK" not in SDK
    assert "every exported identifier" not in SDK


def test_the_surface_is_referred_to_by_name_and_not_by_a_section_number():
    """The surface section lives in another file now, so a number dangles."""
    assert "Section 8" not in SDK
    assert "This repository's own rules, below" in SDK


def test_the_gotchas_are_traps_and_not_a_copy_of_the_requirements():
    """Section 8 exists because the SDKs already IMPLEMENT the requirements.

    What earns a place is a trap: somewhere an ordinary pull request quietly
    un-implements a requirement and the diff still reads as correct. A
    requirement an already-compliant SDK cannot drift away from is reference
    material, and it stays in the requirements document that section 8 cites.
    """
    assert "Gotchas: where a compliant SDK drifts" in SDK
    assert "This section is not a\nchecklist of it" in SDK


def test_the_gotchas_keep_the_traps_that_cost_the_most():
    for trap in [
        # Exactness dies at decode; converting afterwards only locks the error in.
        "Decimal(str(value))",
        # A refusal rendered as no-data truncates a customer's history silently.
        "it must throw",
        "200 and 203 are both success",
        # A new batch helper that writes its own loop breaks the shared pool.
        "sliding window",
        # Reading the client snapshot per request is a race no test will catch.
        "not a per-request answer",
        # Retrying a 429 reads as resilience and spends the customer's credits.
        "Never a 4xx",
        "not configurable",
    ]:
        assert trap in SDK, trap


def test_the_exempt_sdks_are_told_not_to_add_a_decimal_library():
    """The well-meant pull request this rule exists to stop."""
    assert "adding a third-party decimal library to Go, PHP or JavaScript" in SDK


def test_the_gotchas_do_not_renumber_the_repository_surface_section():
    """The repository's own surface section follows this file, so it is 9."""
    assert "## 8. Gotchas" in SDK
    assert "## 9." not in SDK


def test_the_sdk_rules_do_not_carry_the_rating_tiers():
    """They are layered on the default, which must stay the one source."""
    assert "exemplary" not in SDK


@pytest.mark.parametrize("name", ["default", "sdk"])
def test_a_repository_can_ask_for_either_rule_set_alone(name):
    resolved = brief.resolve_review(f"@include {name}\n\nHouse rules.\n")
    assert resolved.includes == [name]
    assert "House rules." in resolved.text


def test_the_pair_an_sdk_repository_asks_for_resolves():
    resolved = brief.resolve_review("@include default\n@include sdk\n\n## The public surface\n")
    assert resolved.includes == ["default", "sdk"]
    assert "Standing review instructions" in resolved.text
    assert "SDK pull request rules" in resolved.text
    assert "## The public surface" in resolved.text
