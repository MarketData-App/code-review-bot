"""Tests for the pure parts of fact gathering: the diff cap and glob matching.

The cap keeps whole files. A half-written hunk would make the model reason
about code it cannot see and call it missing (spec section 9).

Run: pytest tests/test_facts.py
"""

from reviewbot import facts

DIFF = (
    "diff --git a/small.py b/small.py\n"
    "--- a/small.py\n+++ b/small.py\n@@ -1 +1 @@\n-one\n+two\n"
    "diff --git a/big.py b/big.py\n"
    "--- a/big.py\n+++ b/big.py\n@@ -1 +1 @@\n-" + ("x" * 4000) + "\n+y\n"
    "diff --git a/last.py b/last.py\n"
    "--- a/last.py\n+++ b/last.py\n@@ -1 +1 @@\n-a\n+b\n"
)


def test_a_small_diff_is_kept_whole():
    kept, unseen = facts.cap_diff(DIFF, max_kb=400)
    assert kept == DIFF
    assert unseen == []


def test_the_cap_drops_whole_files_and_names_them():
    kept, unseen = facts.cap_diff(DIFF, max_kb=1)
    assert "small.py" in kept
    assert "big.py" not in kept
    assert unseen == ["big.py", "last.py"]


def test_the_cap_never_splits_a_file_section():
    kept, _ = facts.cap_diff(DIFF, max_kb=1)
    assert "x" * 4000 not in kept
    assert kept.endswith("+two\n")


def test_an_empty_diff_caps_to_nothing():
    assert facts.cap_diff("", max_kb=400) == ("", [])


def test_text_before_the_first_header_is_kept():
    kept, _ = facts.cap_diff("preamble\n" + DIFF, max_kb=400)
    assert kept.startswith("preamble\n")


def test_a_single_oversized_file_is_dropped_not_truncated():
    one = "diff --git a/big.py b/big.py\n@@\n" + "z" * 5000 + "\n"
    kept, unseen = facts.cap_diff(one, max_kb=1)
    assert kept.strip() == ""
    assert unseen == ["big.py"]


def test_path_matches_a_plain_glob():
    assert facts.path_matches("a.lock", "*.lock")
    assert not facts.path_matches("dir/a.lock", "*.lock")


def test_double_star_crosses_directories():
    assert facts.path_matches("a/b/c.lock", "**/*.lock")
    assert facts.path_matches("c.lock", "**/*.lock")
    assert facts.path_matches("x/dist/y/z.js", "**/dist/**")
    assert not facts.path_matches("x/distinct/y.js", "**/dist/**")


def test_a_directory_prefix_pattern_matches_below_it():
    assert facts.path_matches("sdk/client/orders.py", "sdk/**")
    assert not facts.path_matches("sdkx/client.py", "sdk/**")


def test_a_bracket_class_works():
    assert facts.path_matches("v2.py", "v[12].py")
    assert not facts.path_matches("v3.py", "v[12].py")


def test_matches_any_is_false_for_no_patterns():
    assert not facts.matches_any("a.py", [])


def test_all_match_requires_every_path():
    assert facts.all_match(["docs/a.md", "docs/b.md"], ["docs/**"])
    assert not facts.all_match(["docs/a.md", "src/b.py"], ["docs/**"])


def test_all_match_is_false_when_no_patterns_are_configured():
    # An empty allow list means "nobody", never "everybody".
    assert not facts.all_match(["docs/a.md"], [])


def test_prfacts_is_constructible_and_frozen():
    item = facts.PRFacts(
        number=7,
        title="t",
        body="b",
        author="me",
        author_is_bot=False,
        draft=False,
        labels=[],
        head_sha="a" * 40,
        base_ref="main",
        node_id="PR_1",
        author_association="MEMBER",
        changed_files=[],
        diff="",
        unseen_files=[],
        ci_state="none",
        previous_comment=None,
        previous_state={},
        comments_since=[],
    )
    assert item.number == 7
    try:
        item.number = 8
    except Exception as exc:  # frozen dataclasses raise FrozenInstanceError
        assert "number" in str(exc) or "frozen" in str(exc).lower()
    else:
        raise AssertionError("PRFacts must be frozen")


def test_prfacts_exposes_the_changed_paths():
    item = facts.PRFacts(
        number=7,
        title="t",
        body="b",
        author="me",
        author_is_bot=False,
        draft=False,
        labels=[],
        head_sha="a" * 40,
        base_ref="main",
        node_id="PR_1",
        author_association="MEMBER",
        changed_files=[{"path": "a.py", "status": "modified", "additions": 1, "deletions": 0}],
        diff="",
        unseen_files=[],
        ci_state="none",
        previous_comment=None,
        previous_state={},
        comments_since=[],
    )
    assert item.paths == ["a.py"]


# --- ignore_paths and the diff budget --------------------------------------
#
# Found by the sdk-py agent: ignore_paths was read in exactly one place, the
# all-match skip. It did nothing for the diff budget. A pull request touching
# one 5.2 MB fixture spent the whole budget on a file nobody wanted reviewed,
# hid every file after it, and then could never be ready because
# allow_ready_with_unseen_files is false.

BIG_FIXTURE = (
    "diff --git a/tests/fixtures/news.json b/tests/fixtures/news.json\n@@\n+" + ("x" * 4000) + "\n"
)
SOURCE = "diff --git a/src/client.py b/src/client.py\n@@\n+    retry()\n"


def test_an_ignored_file_does_not_spend_the_diff_budget():
    kept, unseen = facts.cap_diff(
        BIG_FIXTURE + SOURCE, max_kb=1, ignore_paths=["tests/fixtures/**"]
    )
    assert "src/client.py" in kept
    assert "+    retry()" in kept
    assert unseen == []


def test_an_ignored_file_is_not_reported_as_unseen():
    # It is excluded by policy, not hidden by accident. Calling it unseen
    # would block `ready` for a file the repository asked us to ignore.
    _, unseen = facts.cap_diff(BIG_FIXTURE, max_kb=1, ignore_paths=["tests/fixtures/**"])
    assert unseen == []


def test_an_ignored_file_is_dropped_from_the_diff_entirely():
    kept, _ = facts.cap_diff(BIG_FIXTURE + SOURCE, max_kb=400, ignore_paths=["tests/fixtures/**"])
    assert "tests/fixtures/news.json" not in kept
    assert "src/client.py" in kept


def test_without_ignore_paths_the_old_behaviour_holds():
    kept, unseen = facts.cap_diff(BIG_FIXTURE + SOURCE, max_kb=1)
    assert "src/client.py" not in kept
    assert unseen == ["tests/fixtures/news.json", "src/client.py"]


def test_a_real_overflow_still_reports_unseen_files():
    big_source = "diff --git a/src/huge.py b/src/huge.py\n@@\n+" + ("y" * 4000) + "\n"
    kept, unseen = facts.cap_diff(big_source + SOURCE, max_kb=1, ignore_paths=["tests/fixtures/**"])
    assert unseen == ["src/huge.py", "src/client.py"]
    assert kept.strip() == ""
