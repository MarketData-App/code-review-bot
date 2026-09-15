"""Tests for policy loading, layering and validation.

A malformed policy.yml fails the run with a clear error. It never falls back
to the defaults silently (spec section 7).

Run: pytest tests/test_config.py
"""

import pytest

from reviewbot import config


def test_defaults_match_the_shipped_file():
    policy = config.defaults()
    assert policy["mode"] == "first"
    assert policy["backends"] == ["claude", "codex"]
    assert policy["models"]["claude"] == "claude-opus-5"
    assert policy["auto_merge"]["enabled"] is False


def test_defaults_are_a_fresh_copy_each_call():
    first = config.defaults()
    first["backends"].append("nonsense")
    assert config.defaults()["backends"] == ["claude", "codex"]


def test_none_gives_the_defaults():
    assert config.load(None) == config.defaults()


def test_empty_file_gives_the_defaults():
    assert config.load("") == config.defaults()
    assert config.load("# just a comment\n") == config.defaults()


def test_scalar_override_replaces_one_key_only():
    policy = config.load("mode: all\n")
    assert policy["mode"] == "all"
    assert policy["gate"] is True


def test_nested_map_merges_key_by_key():
    policy = config.load("models:\n  codex: gpt-5.6-mini\n")
    assert policy["models"]["codex"] == "gpt-5.6-mini"
    assert policy["models"]["claude"] == "claude-opus-5"


def test_list_replaces_whole():
    policy = config.load("ignore_paths: ['docs/**']\n")
    assert policy["ignore_paths"] == ["docs/**"]


def test_auto_merge_partial_override_keeps_the_rest():
    policy = config.load("auto_merge:\n  enabled: true\n  authors: [sdk-bot]\n")
    assert policy["auto_merge"] == {"enabled": True, "method": "squash", "authors": ["sdk-bot"]}


def test_unknown_top_level_key_is_rejected():
    with pytest.raises(config.PolicyError) as excinfo:
        config.load("reviw_drafts: true\n")
    assert "reviw_drafts" in str(excinfo.value)


def test_unknown_nested_key_is_rejected():
    with pytest.raises(config.PolicyError) as excinfo:
        config.load("auto_merge:\n  enable: true\n")
    assert "auto_merge.enable" in str(excinfo.value)


def test_wrong_type_is_rejected_with_the_key_named():
    with pytest.raises(config.PolicyError) as excinfo:
        config.load("gate: yes please\n")
    assert "gate" in str(excinfo.value)


def test_unknown_mode_is_rejected():
    with pytest.raises(config.PolicyError) as excinfo:
        config.load("mode: sometimes\n")
    assert "sometimes" in str(excinfo.value)


def test_unknown_backend_is_rejected():
    with pytest.raises(config.PolicyError) as excinfo:
        config.load("backends: [claude, gemini]\n")
    assert "gemini" in str(excinfo.value)


def test_unknown_merge_method_is_rejected():
    with pytest.raises(config.PolicyError):
        config.load("auto_merge:\n  method: fast-forward\n")


def test_empty_backend_list_is_rejected():
    with pytest.raises(config.PolicyError):
        config.load("backends: []\n")


def test_broken_yaml_is_rejected():
    with pytest.raises(config.PolicyError) as excinfo:
        config.load("backends: [claude\n")
    assert "YAML" in str(excinfo.value)


def test_top_level_must_be_a_mapping():
    with pytest.raises(config.PolicyError):
        config.load("- claude\n- codex\n")


def test_positive_number_keys_are_checked():
    with pytest.raises(config.PolicyError) as excinfo:
        config.load("max_diff_kb: 0\n")
    assert "max_diff_kb" in str(excinfo.value)


# --- the gate's width is not a free parameter ------------------------------


def test_a_repo_cannot_widen_the_gate_to_everyone():
    # trusted_associations is the security boundary. A caller repo may narrow
    # it or add COLLABORATOR, but it must not be able to admit the world.
    for bad in ("NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "MANNEQUIN"):
        with pytest.raises(config.PolicyError) as excinfo:
            config.load(f"trusted_associations: [OWNER, {bad}]\n")
        assert bad in str(excinfo.value)


def test_collaborator_may_be_opted_in():
    policy = config.load("trusted_associations: [OWNER, MEMBER, COLLABORATOR]\n")
    assert policy["trusted_associations"] == ["OWNER", "MEMBER", "COLLABORATOR"]


def test_an_unknown_association_is_rejected():
    with pytest.raises(config.PolicyError):
        config.load("trusted_associations: [OWNER, ADMIRAL]\n")


def test_the_association_list_may_not_be_empty():
    with pytest.raises(config.PolicyError):
        config.load("trusted_associations: []\n")
