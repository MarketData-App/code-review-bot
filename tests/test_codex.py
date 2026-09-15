"""Tests for the Codex backend.

Same schema, same errors, same retry as Claude, so the renderer never learns
which one ran.

Run: pytest tests/test_codex.py
"""

import json

import pytest

from reviewbot import config
from reviewbot.backends import base
from tests.conftest import VALID_RESULT, codex_script, write_script

VALID_JSON = json.dumps(VALID_RESULT)


@pytest.fixture
def policy():
    return config.defaults()


def test_codex_needs_the_binary(policy, bin_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert base.build("codex", policy, str(tmp_path)).probe() is False


def test_codex_needs_a_credential(policy, bin_dir, monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    write_script(bin_dir, "codex", codex_script(VALID_JSON))
    assert base.build("codex", policy, str(tmp_path)).probe() is False


def test_an_api_key_makes_codex_live(policy, bin_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    write_script(bin_dir, "codex", codex_script(VALID_JSON))
    assert base.build("codex", policy, str(tmp_path)).probe() is True


def test_a_login_file_makes_codex_live(policy, bin_dir, monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(home))
    write_script(bin_dir, "codex", codex_script(VALID_JSON))
    assert base.build("codex", policy, str(tmp_path)).probe() is True


def test_codex_returns_a_validated_result(policy, bin_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    write_script(bin_dir, "codex", codex_script(VALID_JSON))
    out = base.build("codex", policy, str(tmp_path)).review("BRIEF")
    assert out.backend == "codex"
    assert out.model == "gpt-5.6-sol"
    assert out.result["verdict"]["value"] == "needs_changes"


def test_codex_runs_read_only_and_ephemeral_with_the_schema(policy, bin_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    echo = tmp_path / "echo.json"
    write_script(bin_dir, "codex", codex_script(VALID_JSON, echo_args=str(echo)))
    base.build("codex", policy, str(tmp_path)).review("THE BRIEF")
    seen = json.loads(echo.read_text())
    argv, brief = seen["argv"], seen["brief"]
    assert argv[0] == "exec"
    for flag in ["--skip-git-repo-check", "--ephemeral", "--json", "--output-schema", "-o", "-"]:
        assert flag in argv
    assert argv[argv.index("-s") + 1] == "read-only"
    assert argv[argv.index("-m") + 1] == "gpt-5.6-sol"
    assert 'model_reasoning_effort="high"' in argv
    assert brief.strip() == "THE BRIEF"


def test_the_reasoning_effort_comes_from_policy(policy, bin_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    policy["codex_reasoning_effort"] = "low"
    echo = tmp_path / "echo.json"
    write_script(bin_dir, "codex", codex_script(VALID_JSON, echo_args=str(echo)))
    base.build("codex", policy, str(tmp_path)).review("BRIEF")
    assert 'model_reasoning_effort="low"' in json.loads(echo.read_text())["argv"]


def test_no_output_file_raises(policy, bin_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    write_script(bin_dir, "codex", "import sys; sys.stdin.read(); print('{}')")
    with pytest.raises(base.BackendError) as excinfo:
        base.build("codex", policy, str(tmp_path)).review("BRIEF")
    assert "no output" in str(excinfo.value).lower()


def test_an_error_event_on_stdout_raises(policy, bin_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    script = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "error", "message": "quota exceeded"}))
"""
    write_script(bin_dir, "codex", script)
    with pytest.raises(base.BackendError) as excinfo:
        base.build("codex", policy, str(tmp_path)).review("BRIEF")
    assert "quota exceeded" in str(excinfo.value)


def test_a_nonzero_exit_raises(policy, bin_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    write_script(bin_dir, "codex", codex_script(VALID_JSON, exit_code=2))
    with pytest.raises(base.BackendError):
        base.build("codex", policy, str(tmp_path)).review("BRIEF")


def test_a_malformed_answer_is_retried_once(policy, bin_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    calls = tmp_path / "calls"
    script = f"""
import json, os, sys
brief = sys.stdin.read()
argv = sys.argv[1:]
path = {str(calls)!r}
seen = open(path).read() if os.path.exists(path) else ""
open(path, "a").write("call\\n")
out = argv[argv.index("-o") + 1]
open(out, "w").write(json.dumps({{"summary": "only this"}}) if not seen else {VALID_JSON!r})
print(json.dumps({{"type": "turn.completed"}}))
"""
    write_script(bin_dir, "codex", script)
    out = base.build("codex", policy, str(tmp_path)).review("BRIEF")
    assert out.result["verdict"]["value"] == "needs_changes"
    assert calls.read_text().count("call") == 2
