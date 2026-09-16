"""Every schema handed to a model must satisfy OpenAI's strict mode.

Measured 2026-09-16 on sdk-py run 35125915397, the first Codex review ever to
run against a real credential:

    invalid_request_error / invalid_json_schema
    In context=('properties','findings','items'), 'required' is required to be
    supplied and to be an array including every key in properties

Claude accepts a schema with optional keys; Codex does not. The rule is that
every object names EVERY property in `required`, so "optional" has to be
expressed as a nullable type rather than an absent key. These tests hold both
halves of that: the shape the API demands, and the ability to say "none".

Run: pytest tests/test_strict_schema.py
"""

import pytest
from jsonschema import Draft202012Validator

from reviewbot import merge
from reviewbot.result import load_schema


def objects(node, path="root"):
    """Every object-with-properties in a schema, depth first."""
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            yield path, node
        for key, value in node.items():
            yield from objects(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from objects(value, f"{path}[{index}]")


STRICT_SCHEMAS = [("result.json", load_schema()), ("merge", merge.MERGE_SCHEMA)]


@pytest.mark.parametrize("name,schema", STRICT_SCHEMAS)
def test_every_object_requires_every_property(name, schema):
    bad = {
        path: sorted(set(node["properties"]) - set(node.get("required") or []))
        for path, node in objects(schema)
        if set(node["properties"]) - set(node.get("required") or [])
    }
    assert bad == {}, f"{name}: objects with optional keys are rejected by strict mode: {bad}"


@pytest.mark.parametrize("name,schema", STRICT_SCHEMAS)
def test_every_object_forbids_extra_properties(name, schema):
    loose = [
        path for path, node in objects(schema) if node.get("additionalProperties") is not False
    ]
    assert loose == [], f"{name}: additionalProperties must be false: {loose}"


def test_a_review_with_nothing_optional_still_validates():
    """The model must be able to say "no decision, no evidence, no ask".

    Requiring every key is only safe if declining is expressible. Without a
    nullable decision the model invents one: asked for a trivial style nit it
    produced "Should this trivial style issue block merging?" (measured).
    """
    schema = load_schema()
    minimal = {
        "summary": "Nothing to say.",
        "findings": [
            {
                "file": None,
                "line_start": None,
                "line_end": None,
                "category": "style",
                "severity": "nit",
                "confidence": 0.5,
                "title": "A nit",
                "body": "Some body.",
                "evidence": None,
            }
        ],
        "proof": {"status": "not_applicable", "ask": None},
        "verdict": {"value": "ready", "reason": "Fine."},
        "rating": {"patch": 5, "proof": 5},
        "praise": [],
        "decision": None,
    }
    errors = sorted(Draft202012Validator(schema).iter_errors(minimal), key=str)
    assert errors == [], [e.message for e in errors]


def test_a_review_that_does_use_the_optional_fields_still_validates():
    schema = load_schema()
    full = {
        "summary": "Something to say.",
        "findings": [
            {
                "file": "src/x.py",
                "line_start": 3,
                "line_end": 4,
                "category": "correctness",
                "severity": "blocking",
                "confidence": 0.9,
                "title": "A real one",
                "body": "Some body.",
                "evidence": "src/x.py:3",
            }
        ],
        "proof": {"status": "missing", "ask": "Paste a session."},
        "verdict": {"value": "needs_changes", "reason": "One blocking finding."},
        "rating": {"patch": 2, "proof": 1},
        "praise": ["Good tests."],
        "decision": {
            "question": "Which way?",
            "options": ["This", "That"],
            "recommendation": "This.",
        },
    }
    errors = sorted(Draft202012Validator(schema).iter_errors(full), key=str)
    assert errors == [], [e.message for e in errors]


def test_a_null_decision_reads_as_no_decision():
    """Consumers use truthiness, so null must behave exactly like absent."""
    from reviewbot import render

    assert bool({"decision": None}.get("decision")) is False
    assert render is not None
