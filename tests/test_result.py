"""Tests for the fixed result schema and its loader.

Every backend answers against reviewbot/schema/result.json, so the renderer
never learns which model ran. These tests pin the shape.

Run: pytest tests/test_result.py
"""

import json

import pytest

from reviewbot import result as result_mod

VALID = {
    "summary": "Adds a retry to the candles fetch. Small and contained.",
    "findings": [
        {
            "file": "reviewbot/github.py",
            "line_start": 42,
            "line_end": 44,
            "category": "correctness",
            "severity": "blocking",
            "confidence": 0.9,
            "title": "Retry loop never sleeps",
            "body": "The backoff is computed and discarded.",
            "evidence": "reviewbot/github.py:43",
        }
    ],
    "proof": {"status": "missing", "ask": "Show the retry firing once against a 503."},
    "verdict": {"value": "needs_changes", "reason": "One blocking finding."},
    "rating": {"patch": 3, "proof": 2},
    # Both required since the schema went strict: OpenAI rejects a schema whose
    # `required` omits any key, so declining is null, not an absent key.
    "praise": [],
    "decision": None,
}


def test_load_schema_returns_the_object_schema():
    schema = result_mod.load_schema()
    assert schema["type"] == "object"
    assert "findings" in schema["properties"]


def test_valid_result_has_no_errors():
    assert result_mod.validate(VALID) == []


def test_unlocated_finding_is_valid():
    data = json.loads(json.dumps(VALID))
    data["findings"][0]["file"] = None
    data["findings"][0]["line_start"] = None
    data["findings"][0]["line_end"] = None
    assert result_mod.validate(data) == []


def test_missing_verdict_is_reported():
    data = json.loads(json.dumps(VALID))
    del data["verdict"]
    errors = result_mod.validate(data)
    assert errors
    assert any("verdict" in e for e in errors)


def test_unknown_severity_is_reported():
    data = json.loads(json.dumps(VALID))
    data["findings"][0]["severity"] = "urgent"
    errors = result_mod.validate(data)
    assert any("severity" in e for e in errors)


def test_extra_top_level_key_is_reported():
    data = json.loads(json.dumps(VALID))
    data["notes"] = "hello"
    assert result_mod.validate(data)


def test_parse_raises_on_malformed_json():
    with pytest.raises(result_mod.ResultError) as excinfo:
        result_mod.parse("{not json")
    assert "JSON" in str(excinfo.value)


def test_parse_raises_on_schema_failure_and_names_the_field():
    with pytest.raises(result_mod.ResultError) as excinfo:
        result_mod.parse(json.dumps({"summary": "x"}))
    assert "findings" in str(excinfo.value)


def test_parse_returns_the_data_when_valid():
    assert result_mod.parse(json.dumps(VALID))["verdict"]["value"] == "needs_changes"


def test_overall_rating_is_the_weaker_tier():
    assert result_mod.overall_rating({"patch": 5, "proof": 2}) == 2
    assert result_mod.overall_rating({"patch": 1, "proof": 6}) == 1


def test_there_are_six_tier_words_for_each_kind():
    # The tier meanings are schema semantics: the brief and the comment both
    # read them from here, so they can never drift apart.
    assert len(result_mod.PATCH_TIERS) == 6
    assert len(result_mod.PROOF_TIERS) == 6


def test_tier_word_is_one_based():
    assert result_mod.tier_word("patch", 1) == "harmful"
    assert result_mod.tier_word("patch", 6) == "exemplary"
    assert result_mod.tier_word("proof", 1) == "none"
    assert result_mod.tier_word("proof", 6) == "comprehensive"
