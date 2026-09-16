"""The generated SDK rules must survive the loader that will read them.

A REVIEW.md that fails to merge does not fail loudly: `load_review` returns the
repository's text verbatim when the include directive is missing, so the shipped
default instructions vanish and nobody notices until a review comes back thin.
"""

import subprocess
from pathlib import Path

import pytest

from reviewbot import brief

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "docs" / "sdk" / "build.sh"
REPOS = ["sdk-py", "sdk-php", "sdk-go", "sdk-java", "sdk-js", "sdk-csharp"]


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("sdk")
    subprocess.run([str(BUILD), str(out)], check=True, capture_output=True)
    return out


def test_every_sdk_repository_gets_a_file(built):
    assert sorted(p.name for p in built.iterdir()) == sorted(REPOS)


@pytest.mark.parametrize("repo", REPOS)
def test_the_loader_merges_the_default_instructions_in(built, repo):
    merged = brief.load_review((built / repo / "REVIEW.md").read_text())
    assert merged.startswith("# Standing review instructions")
    assert "@include default" not in merged


@pytest.mark.parametrize("repo", REPOS)
def test_the_shared_rules_reach_every_repository(built, repo):
    merged = brief.load_review((built / repo / "REVIEW.md").read_text())
    assert "The version bump is a calculation, not an opinion" in merged
    assert "The bump arrives with the pull request" in merged
    assert "The public surface of this SDK" in merged


def test_only_sdk_py_carries_house_rules(built):
    housed = [r for r in REPOS if "house rules" in (built / r / "REVIEW.md").read_text()]
    assert housed == ["sdk-py"]


def test_go_is_told_its_major_lives_in_the_import_path(built):
    """Go is the one SDK where the bump cannot be deferred to release time."""
    text = (built / "sdk-go" / "REVIEW.md").read_text()
    assert "sdk-go/vN" in text
    for repo in set(REPOS) - {"sdk-go"}:
        assert "/vN" not in (built / repo / "REVIEW.md").read_text()
