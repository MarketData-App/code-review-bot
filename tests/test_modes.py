"""Tests for mode handling with two live backends.

Run: pytest tests/test_modes.py
"""

import json

import pytest

from reviewbot import config
from reviewbot.backends import base
from tests.conftest import VALID_RESULT, claude_script, codex_script, write_script

VALID_JSON = json.dumps(VALID_RESULT)


@pytest.fixture
def both(policy, bin_dir, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token-value")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return bin_dir


@pytest.fixture
def policy():
    return config.defaults()


def test_live_keeps_the_policy_order(policy, both, tmp_path):
    write_script(both, "claude", claude_script(VALID_JSON))
    write_script(both, "codex", codex_script(VALID_JSON))
    policy["backends"] = ["codex", "claude"]
    backends, dropped = base.live(policy, str(tmp_path))
    assert [b.name for b in backends] == ["codex", "claude"]
    assert dropped == []


def test_first_mode_runs_only_the_first_live_backend(policy, both, tmp_path):
    codex_echo = tmp_path / "codex-ran.json"
    write_script(both, "claude", claude_script(VALID_JSON))
    write_script(both, "codex", codex_script(VALID_JSON, echo_args=str(codex_echo)))
    results, missing = base.run(policy, "BRIEF", str(tmp_path))
    assert [r.backend for r in results] == ["claude"]
    assert missing == ["codex"]
    assert not codex_echo.exists()


def test_fallback_mode_moves_to_the_next_backend(policy, both, tmp_path):
    policy["mode"] = "fallback"
    write_script(both, "claude", claude_script(VALID_JSON, exit_code=1))
    write_script(both, "codex", codex_script(VALID_JSON))
    results, missing = base.run(policy, "BRIEF", str(tmp_path))
    assert [r.backend for r in results] == ["codex"]
    assert missing == ["claude"]


def test_fallback_mode_fails_when_every_backend_fails(policy, both, tmp_path):
    policy["mode"] = "fallback"
    write_script(both, "claude", claude_script(VALID_JSON, exit_code=1))
    write_script(both, "codex", codex_script(VALID_JSON, exit_code=1))
    with pytest.raises(base.BackendError):
        base.run(policy, "BRIEF", str(tmp_path))


def test_all_mode_runs_both_and_orders_by_policy(policy, both, tmp_path):
    policy["mode"] = "all"
    write_script(both, "claude", claude_script(VALID_JSON))
    write_script(both, "codex", codex_script(VALID_JSON))
    results, missing = base.run(policy, "BRIEF", str(tmp_path))
    assert [r.backend for r in results] == ["claude", "codex"]
    assert missing == []


def test_all_mode_posts_from_the_survivor(policy, both, tmp_path):
    policy["mode"] = "all"
    write_script(both, "claude", claude_script(VALID_JSON))
    write_script(both, "codex", codex_script(VALID_JSON, exit_code=1))
    results, missing = base.run(policy, "BRIEF", str(tmp_path))
    assert [r.backend for r in results] == ["claude"]
    assert missing == ["codex"]
