"""Tests for what the log says when a backend does not answer.

The footer tells a reader that a backend was `unavailable`. It cannot say why,
and for a long time neither could anything else: `base.run` caught
`BackendError` and kept only the name. Eleven consecutive sdk-py reviews ran
without Claude and no record anywhere held the reason.

These tests pin the two lines that close that gap, and the difference between
them: a backend that never started reads differently from one that started and
failed.

Run: pytest tests/test_backend_reporting.py
"""

import json

import pytest

from reviewbot import config
from reviewbot.backends import base
from tests.conftest import VALID_RESULT, claude_script, codex_script, write_script

VALID_JSON = json.dumps(VALID_RESULT)


@pytest.fixture
def policy():
    return config.defaults()


@pytest.fixture
def both(policy, bin_dir, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token-value")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return bin_dir


def test_a_dropped_backend_says_it_never_started(policy, bin_dir, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token-value")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    write_script(bin_dir, "claude", claude_script(VALID_JSON))
    base.live(policy, str(bin_dir))
    out = capsys.readouterr().out
    assert "codex" in out
    assert "not installed or not authenticated" in out


def test_a_failing_backend_reports_its_reason(policy, both, tmp_path, capsys):
    policy["mode"] = "all"
    write_script(both, "claude", claude_script(VALID_JSON, exit_code=3))
    write_script(both, "codex", codex_script(VALID_JSON))
    results, missing = base.run(policy, "BRIEF", str(tmp_path))
    assert [r.backend for r in results] == ["codex"]
    assert missing == ["claude"]
    out = capsys.readouterr().out
    assert "claude" in out and "exit 3" in out


def test_a_failing_backend_reads_differently_from_a_dropped_one(policy, both, tmp_path, capsys):
    # The whole point of the change: `claude unavailable` in the footer covers
    # both, and the log must separate them.
    policy["mode"] = "all"
    write_script(both, "claude", claude_script(VALID_JSON, exit_code=3))
    write_script(both, "codex", codex_script(VALID_JSON))
    base.run(policy, "BRIEF", str(tmp_path))
    out = capsys.readouterr().out
    assert "not installed or not authenticated" not in out


def test_fallback_mode_reports_the_backend_it_moved_past(policy, both, tmp_path, capsys):
    policy["mode"] = "fallback"
    write_script(both, "claude", claude_script(VALID_JSON, exit_code=1))
    write_script(both, "codex", codex_script(VALID_JSON))
    results, _ = base.run(policy, "BRIEF", str(tmp_path))
    assert [r.backend for r in results] == ["codex"]
    out = capsys.readouterr().out
    assert "claude" in out and "exit 1" in out


def test_the_usage_limit_message_reaches_the_log(policy, both, tmp_path, capsys):
    # The real sdk-py shape: the CLI exits 0 and reports the refusal in the
    # envelope. This is the message that was being thrown away.
    policy["mode"] = "all"
    script = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "result", "is_error": True,
                  "result": "Claude AI usage limit reached|1758499200"}))
"""
    write_script(both, "claude", script)
    write_script(both, "codex", codex_script(VALID_JSON))
    base.run(policy, "BRIEF", str(tmp_path))
    assert "usage limit reached" in capsys.readouterr().out


def test_fallback_mode_says_a_later_backend_was_never_invoked(policy, both, tmp_path, capsys):
    # THE sdk-py SHAPE, and the one that made this whole change necessary.
    # `backends: [codex, claude]` with `mode: fallback`: codex answers, so
    # claude never runs. The footer still renders `claude unavailable`, which
    # reads as a failure and is not one. Measured 2026-09-21: eleven sdk-py
    # reviews in a row reported it and nothing was wrong.
    policy["mode"] = "fallback"
    policy["backends"] = ["codex", "claude"]
    write_script(both, "claude", claude_script(VALID_JSON))
    write_script(both, "codex", codex_script(VALID_JSON))
    results, missing = base.run(policy, "BRIEF", str(tmp_path))
    assert [r.backend for r in results] == ["codex"]
    assert missing == ["claude"]
    out = capsys.readouterr().out
    assert "claude was not invoked" in out
    assert "codex answered first" in out
    # It must not read as a fault of any kind.
    assert "not installed" not in out
    assert "exit" not in out


def test_first_mode_says_the_other_backends_were_never_invoked(policy, both, tmp_path, capsys):
    policy["mode"] = "first"
    policy["backends"] = ["claude", "codex"]
    write_script(both, "claude", claude_script(VALID_JSON))
    write_script(both, "codex", codex_script(VALID_JSON))
    _, missing = base.run(policy, "BRIEF", str(tmp_path))
    assert missing == ["codex"]
    out = capsys.readouterr().out
    assert "codex was not invoked" in out
    assert "claude answered first" in out


def test_a_token_shaped_reason_is_scrubbed(policy, both, tmp_path, capsys):
    # The reason is CLI output, and the runner holds a borrowed auth.json whose
    # access_token is a JWT. A reason printed raw could carry one.
    policy["mode"] = "all"
    leak = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g"
    script = f"""
import json, sys
sys.stdin.read()
print(json.dumps({{"type": "result", "is_error": True,
                  "result": "auth failed for {leak}"}}))
"""
    write_script(both, "claude", script)
    write_script(both, "codex", codex_script(VALID_JSON))
    base.run(policy, "BRIEF", str(tmp_path))
    out = capsys.readouterr().out
    assert leak not in out
    assert "[redacted]" in out
