"""Tests for the merge in `mode: all`.

One review, one finding per issue, agreement visible (spec section 4.1).
The located merge is deterministic and is tested without any model. The
unlocated merge takes an injected callable, so it is tested the same way.

Run: pytest tests/test_merge.py
"""

import pytest

from reviewbot import config, merge
from reviewbot.backends.base import BackendResult


def finding(
    file="sdk/client.py",
    start=42,
    end=None,
    category="correctness",
    severity="should_fix",
    confidence=0.7,
    title="Retry loop never sleeps",
    body="The backoff is discarded.",
):
    return {
        "file": file,
        "line_start": start,
        "line_end": end,
        "category": category,
        "severity": severity,
        "confidence": confidence,
        "title": title,
        "body": body,
        "evidence": "e",
    }


def result(
    findings,
    verdict="needs_changes",
    proof="sufficient",
    patch=4,
    proof_tier=5,
    summary="A summary.",
    praise=None,
    decision=None,
):
    out = {
        "summary": summary,
        "findings": findings,
        "proof": {"status": proof, "ask": "ask"},
        "verdict": {"value": verdict, "reason": "r"},
        "rating": {"patch": patch, "proof": proof_tier},
        "praise": praise or [],
        "decision": None,
    }
    if decision:
        out["decision"] = decision
    return out


def backend_result(name, data, model="m"):
    return BackendResult(backend=name, model=model, result=data)


@pytest.fixture
def policy():
    return config.defaults()


# --- one backend -----------------------------------------------------------


def test_one_result_passes_through_with_ids_and_tags(policy):
    out = merge.merge([backend_result("claude", result([finding()]))], policy)
    item = out["findings"][0]
    assert item["backends"] == ["claude"]
    assert item["agreed"] is True
    assert len(item["id"]) == 8


def test_one_result_keeps_its_verdict_and_rating(policy):
    out = merge.merge([backend_result("claude", result([], verdict="ready"))], policy)
    assert out["verdict"]["value"] == "ready"
    assert out["rating"] == {"patch": 4, "proof": 5}


# --- located findings ------------------------------------------------------


def test_the_same_line_in_the_same_file_merges(policy):
    a = backend_result("claude", result([finding(start=42)]))
    b = backend_result("codex", result([finding(start=42, title="Backoff is ignored")]))
    out = merge.merge([a, b], policy)
    assert len(out["findings"]) == 1
    assert out["findings"][0]["backends"] == ["claude", "codex"]
    assert out["findings"][0]["agreed"] is True


def test_lines_within_three_merge(policy):
    a = backend_result("claude", result([finding(start=42)]))
    b = backend_result("codex", result([finding(start=45)]))
    assert len(merge.merge([a, b], policy)["findings"]) == 1


def test_lines_further_apart_stay_separate(policy):
    a = backend_result("claude", result([finding(start=42)]))
    b = backend_result("codex", result([finding(start=60)]))
    assert len(merge.merge([a, b], policy)["findings"]) == 2


def test_overlapping_ranges_merge(policy):
    a = backend_result("claude", result([finding(start=40, end=50)]))
    b = backend_result("codex", result([finding(start=48, end=60)]))
    merged = merge.merge([a, b], policy)["findings"]
    assert len(merged) == 1
    assert merged[0]["line_start"] == 40
    assert merged[0]["line_end"] == 60


def test_a_different_file_never_merges(policy):
    a = backend_result("claude", result([finding(file="a.py")]))
    b = backend_result("codex", result([finding(file="b.py")]))
    assert len(merge.merge([a, b], policy)["findings"]) == 2


def test_a_different_category_never_merges(policy):
    a = backend_result("claude", result([finding(category="correctness")]))
    b = backend_result("codex", result([finding(category="tests")]))
    assert len(merge.merge([a, b], policy)["findings"]) == 2


def test_the_merged_finding_keeps_the_higher_confidence_wording(policy):
    a = backend_result("claude", result([finding(confidence=0.4, title="Vague")]))
    b = backend_result("codex", result([finding(confidence=0.9, title="Precise")]))
    assert merge.merge([a, b], policy)["findings"][0]["title"] == "Precise"


def test_the_merged_finding_keeps_the_higher_severity(policy):
    a = backend_result("claude", result([finding(severity="nit", confidence=0.9)]))
    b = backend_result("codex", result([finding(severity="blocking", confidence=0.2)]))
    assert merge.merge([a, b], policy)["findings"][0]["severity"] == "blocking"


def test_a_single_backend_finding_is_not_agreed(policy):
    a = backend_result("claude", result([finding(start=42)]))
    b = backend_result("codex", result([finding(start=90, title="Something else")]))
    merged = {f["title"]: f for f in merge.merge([a, b], policy)["findings"]}
    assert merged["Retry loop never sleeps"]["agreed"] is False
    assert merged["Something else"]["backends"] == ["codex"]


def test_two_findings_from_one_backend_on_the_same_line_do_not_self_merge(policy):
    a = backend_result(
        "claude", result([finding(start=42), finding(start=43, title="Other issue")])
    )
    assert len(merge.merge([a], policy)["findings"]) == 2


# --- unlocated findings ----------------------------------------------------


def test_unlocated_findings_go_to_the_merger(policy):
    seen = {}

    def merger(items):
        seen["count"] = len(items)
        return [finding(file=None, start=None, title="One issue")]

    a = backend_result("claude", result([finding(file=None, start=None, title="A")]))
    b = backend_result("codex", result([finding(file=None, start=None, title="B")]))
    out = merge.merge([a, b], policy, unlocated_merger=merger)
    assert seen["count"] == 2
    assert [f["title"] for f in out["findings"]] == ["One issue"]


def test_the_merger_is_not_called_without_unlocated_findings(policy):
    def merger(items):
        raise AssertionError("must not be called")

    a = backend_result("claude", result([finding()]))
    b = backend_result("codex", result([finding()]))
    merge.merge([a, b], policy, unlocated_merger=merger)


def test_the_merger_is_not_called_for_a_single_backend(policy):
    def merger(items):
        raise AssertionError("must not be called")

    a = backend_result("claude", result([finding(file=None, start=None)]))
    merge.merge([a], policy, unlocated_merger=merger)


def test_a_failing_merger_falls_back_to_deterministic_dedup(policy):
    def merger(items):
        raise RuntimeError("the model call failed")

    a = backend_result("claude", result([finding(file=None, start=None, title="Same title")]))
    b = backend_result("codex", result([finding(file=None, start=None, title="same TITLE")]))
    out = merge.merge([a, b], policy, unlocated_merger=merger)
    assert len(out["findings"]) == 1
    assert out["findings"][0]["backends"] == ["claude", "codex"]


def test_without_a_merger_identical_titles_still_dedup(policy):
    a = backend_result("claude", result([finding(file=None, start=None, title="Same")]))
    b = backend_result("codex", result([finding(file=None, start=None, title="Same")]))
    assert len(merge.merge([a, b], policy)["findings"]) == 1


# --- verdict, proof, rating, the rest --------------------------------------


def test_the_weaker_verdict_wins(policy):
    a = backend_result("claude", result([], verdict="ready"))
    b = backend_result("codex", result([], verdict="blocked"))
    out = merge.merge([a, b], policy)
    assert out["verdict"]["value"] == "blocked"
    assert "codex" in out["verdict"]["reason"]


def test_the_weaker_proof_status_wins(policy):
    a = backend_result("claude", result([], proof="sufficient"))
    b = backend_result("codex", result([], proof="missing"))
    assert merge.merge([a, b], policy)["proof"]["status"] == "missing"


def test_the_proof_ask_comes_from_the_weaker_side(policy):
    a = backend_result("claude", result([], proof="sufficient"))
    weak = result([], proof="missing")
    weak["proof"]["ask"] = "Show the 503 retry."
    b = backend_result("codex", weak)
    assert merge.merge([a, b], policy)["proof"]["ask"] == "Show the 503 retry."


def test_the_weaker_rating_wins_per_tier(policy):
    a = backend_result("claude", result([], patch=5, proof_tier=2))
    b = backend_result("codex", result([], patch=3, proof_tier=6))
    assert merge.merge([a, b], policy)["rating"] == {"patch": 3, "proof": 2}


def test_the_summary_comes_from_the_first_backend_in_policy_order(policy):
    a = backend_result("claude", result([], summary="Claude says this."))
    b = backend_result("codex", result([], summary="Codex says that."))
    assert merge.merge([b, a], policy)["summary"] == "Claude says this."


def test_praise_is_combined_without_duplicates(policy):
    a = backend_result("claude", result([], praise=["Good test.", "Nice name."]))
    b = backend_result("codex", result([], praise=["Good test."]))
    assert merge.merge([a, b], policy)["praise"] == ["Good test.", "Nice name."]


def test_the_first_decision_packet_wins(policy):
    a = backend_result("claude", result([]))
    b = backend_result(
        "codex",
        result([], decision={"question": "q", "options": ["a", "b"], "recommendation": "a"}),
    )
    assert merge.merge([a, b], policy)["decision"]["question"] == "q"


def test_the_merged_result_still_matches_the_schema(policy):
    from reviewbot import result as result_mod

    a = backend_result("claude", result([finding()]))
    b = backend_result("codex", result([finding(start=90, title="Other")]))
    merged = merge.merge([a, b], policy)
    stripped = dict(
        merged,
        findings=[
            {k: v for k, v in f.items() if k not in ("id", "backends", "agreed")}
            for f in merged["findings"]
        ],
    )
    assert result_mod.validate(stripped) == []


def test_weaker_helpers_are_symmetric():
    assert merge.weaker_verdict("ready", "needs_changes") == "needs_changes"
    assert merge.weaker_verdict("needs_changes", "ready") == "needs_changes"
    assert merge.weaker_proof("not_applicable", "sufficient") == "sufficient"
    assert merge.weaker_proof("insufficient", "missing") == "missing"
