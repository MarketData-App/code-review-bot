"""Tests for the hidden state carried in the bot's comment.

The comment and the check run are the only state the bot keeps, so the
markers must survive an edit round trip and must never break the comment when
a human edits around them.

Run: pytest tests/test_markers.py
"""

from reviewbot import markers

STATE = {
    "reviewed_sha": "a" * 40,
    "revision": 3,
    "backends": ["claude"],
    "proof_status": "missing",
    "decision_open": False,
    "finding_ids": ["1234abcd", "5678efab"],
    "waived": {"1234abcd": "selden"},
}


def test_emit_contains_the_marker():
    assert markers.MARKER in markers.emit(STATE)


def test_emit_is_html_comments_only():
    for line in markers.emit(STATE).strip().splitlines():
        assert line.startswith("<!--") and line.endswith("-->")


def test_round_trip_returns_the_same_state():
    assert markers.parse(f"Some comment body\n\n{markers.emit(STATE)}") == STATE


def test_parse_of_a_plain_comment_is_empty():
    assert markers.parse("Looks good to me") == {}


def test_parse_of_none_is_empty():
    assert markers.parse(None) == {}


def test_parse_survives_a_broken_state_line():
    body = f"text\n{markers.MARKER}\n<!-- reviewbot-state: {{not json -->\n"
    assert markers.parse(body) == {}


def test_parse_takes_the_last_state_when_a_human_quoted_an_older_one():
    body = markers.emit({"revision": 1}) + "\nquoted above\n" + markers.emit({"revision": 2})
    assert markers.parse(body)["revision"] == 2


def test_is_bot_comment_follows_the_marker():
    assert markers.is_bot_comment(markers.emit(STATE))
    assert not markers.is_bot_comment("a human wrote this")
    assert not markers.is_bot_comment(None)


def test_state_json_stays_on_one_line():
    # A multi-line JSON blob would break the single-line regex the parser uses.
    state = dict(STATE, finding_ids=[f"{i:08x}" for i in range(40)])
    assert len([x for x in markers.emit(state).splitlines() if x.strip()]) == 2
