"""Policy loading: the shipped defaults, the repo's overrides, and validation.

A malformed policy fails the run with a named key. It never falls back to the
defaults silently, because a typo that quietly disables the gate is worse than
a red check run.
"""

import copy
from pathlib import Path

import yaml

DEFAULTS_PATH = Path(__file__).parent / "defaults" / "policy.yml"

BACKENDS = ("claude", "codex")
MODES = ("first", "fallback", "all")
MERGE_METHODS = ("squash", "merge", "rebase")
EFFORTS = ("low", "medium", "high")

# key -> expected type. Nested maps are described by their own table below.
_TOP_TYPES = {
    "backends": list,
    "mode": str,
    "require_agreement": bool,
    "models": dict,
    "codex_reasoning_effort": str,
    "gate": bool,
    "require_ci_green": bool,
    "proof": dict,
    "ratings": bool,
    "decision_packets": bool,
    "auto_approve": bool,
    "auto_approve_paths": list,
    "auto_merge": dict,
    "review_drafts": bool,
    "ignore_paths": list,
    "ignore_authors": list,
    "check_name": str,
    "max_diff_kb": int,
    "timeout_minutes": int,
    "allow_ready_with_unseen_files": bool,
}

_NESTED_TYPES = {
    "models": {"claude": str, "codex": str},
    "proof": {"required": bool, "paths": list},
    "auto_merge": {"enabled": bool, "method": str, "authors": list},
}

_defaults_cache: dict | None = None


class PolicyError(ValueError):
    """A policy file is malformed. The message names the key."""


def defaults() -> dict:
    """The shipped defaults, as a fresh deep copy the caller may mutate."""
    global _defaults_cache
    if _defaults_cache is None:
        _defaults_cache = yaml.safe_load(DEFAULTS_PATH.read_text())
    return copy.deepcopy(_defaults_cache)


def load(text: str | None) -> dict:
    """Layer a repo policy.yml over the defaults and validate the result."""
    policy = defaults()
    if text is None or not text.strip():
        return policy
    try:
        override = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyError(f"policy.yml is not valid YAML: {exc}") from exc
    if override is None:
        return policy
    if not isinstance(override, dict):
        raise PolicyError("policy.yml must be a mapping of settings at the top level")
    _check_keys(override)
    _merge(policy, override)
    _validate(policy)
    return policy


def _check_keys(override: dict) -> None:
    """Reject unknown keys before merging, so a typo is never silently kept."""
    for key, value in override.items():
        if key not in _TOP_TYPES:
            raise PolicyError(f"unknown policy key: {key}")
        if key in _NESTED_TYPES and isinstance(value, dict):
            for nested in value:
                if nested not in _NESTED_TYPES[key]:
                    raise PolicyError(f"unknown policy key: {key}.{nested}")


def _merge(base: dict, override: dict) -> None:
    """Deep-merge maps; replace everything else, lists included."""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def _check_type(key: str, value, expected: type) -> None:
    # bool is a subclass of int; an int key must not accept True.
    if expected is int and isinstance(value, bool):
        raise PolicyError(f"{key} must be a number, got a boolean")
    if not isinstance(value, expected):
        raise PolicyError(f"{key} must be {expected.__name__}, got {type(value).__name__}")


def _validate(policy: dict) -> None:
    for key, expected in _TOP_TYPES.items():
        _check_type(key, policy[key], expected)
    for parent, table in _NESTED_TYPES.items():
        for key, expected in table.items():
            _check_type(f"{parent}.{key}", policy[parent][key], expected)

    if not policy["backends"]:
        raise PolicyError("backends must name at least one backend")
    for name in policy["backends"]:
        if name not in BACKENDS:
            raise PolicyError(f"unknown backend: {name}; choose from {', '.join(BACKENDS)}")
    if policy["mode"] not in MODES:
        raise PolicyError(f"unknown mode: {policy['mode']}; choose from {', '.join(MODES)}")
    if policy["auto_merge"]["method"] not in MERGE_METHODS:
        raise PolicyError(
            f"unknown auto_merge.method: {policy['auto_merge']['method']}; "
            f"choose from {', '.join(MERGE_METHODS)}"
        )
    if policy["codex_reasoning_effort"] not in EFFORTS:
        raise PolicyError(
            f"unknown codex_reasoning_effort: {policy['codex_reasoning_effort']}; "
            f"choose from {', '.join(EFFORTS)}"
        )
    for key in ("max_diff_kb", "timeout_minutes"):
        if policy[key] < 1:
            raise PolicyError(f"{key} must be 1 or more")
    for key in ("ignore_paths", "ignore_authors", "auto_approve_paths"):
        for item in policy[key]:
            if not isinstance(item, str):
                raise PolicyError(f"{key} must hold strings only")
    for item in policy["proof"]["paths"] + policy["auto_merge"]["authors"]:
        if not isinstance(item, str):
            raise PolicyError("proof.paths and auto_merge.authors must hold strings only")
