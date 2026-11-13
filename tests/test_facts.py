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


# --- the diff rebuilt from the per-file patches (issue #32) -----------------


def listed(path, status="modified", patch="@@ -1 +1 @@\n-a\n+b", **extra):
    """One entry of `pulls/N/files` as the API returns it."""
    item = {"filename": path, "status": status, "additions": 1, "deletions": 1}
    if patch is not None:
        item["patch"] = patch
    item.update(extra)
    return item


def rebuilt_diff(files, rebuilt=None):
    """`sections_from_files` as one text, and the paths it omitted."""
    sections, omitted = facts.sections_from_files(files, rebuilt)
    return "".join(text for _, text in sections), omitted


def test_the_rebuilt_diff_has_one_git_section_per_file():
    diff, omitted = rebuilt_diff(
        [
            listed("sdk/client.py"),
            listed("sdk/new.py", status="added", patch="@@ -0,0 +1 @@\n+fresh"),
            listed("sdk/old.py", status="removed", patch="@@ -1 +0,0 @@\n-gone"),
        ]
    )
    assert omitted == []
    assert diff == (
        "diff --git a/sdk/client.py b/sdk/client.py\n"
        "--- a/sdk/client.py\n+++ b/sdk/client.py\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/sdk/new.py b/sdk/new.py\n"
        "--- /dev/null\n+++ b/sdk/new.py\n@@ -0,0 +1 @@\n+fresh\n"
        "diff --git a/sdk/old.py b/sdk/old.py\n"
        "--- a/sdk/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n"
    )


def test_the_rebuilt_diff_names_both_sides_of_a_rename():
    diff, _ = rebuilt_diff(
        [listed("sdk/after.py", status="renamed", previous_filename="sdk/before.py")]
    )
    assert diff.startswith(
        "diff --git a/sdk/before.py b/sdk/after.py\n"
        "rename from sdk/before.py\nrename to sdk/after.py\n"
        "--- a/sdk/before.py\n+++ b/sdk/after.py\n"
    )


def test_a_file_without_a_patch_is_omitted_and_never_reads_as_unchanged():
    """Measured on api#739: past about 900 KB of patches GitHub sent the last
    nine files with no patch and 0/0 line counts, pyproject.toml among them.
    So a missing patch is "not seen", whatever the counts say."""
    diff, omitted = rebuilt_diff(
        [
            listed("big.py", status="added", patch=None, additions=6907, deletions=0),
            listed("pyproject.toml", patch=None, additions=0, deletions=0),
            listed("small.py"),
        ]
    )
    assert omitted == ["big.py", "pyproject.toml"]
    assert "diff --git a/pyproject.toml b/pyproject.toml\n" + facts.NO_PATCH_NOTE + "\n" in diff
    assert "+b\n" in diff


def test_the_cap_splits_the_rebuilt_diff_by_file():
    sections, _ = facts.sections_from_files(
        [listed("small.py"), listed("big.py", patch="@@ -1 +1 @@\n+" + "y" * 3000), listed("z.py")]
    )
    kept, unseen = facts.cap_sections(sections, max_kb=1)
    assert "small.py" in kept
    assert unseen == ["big.py", "z.py"]


# --- a patch rebuilt from the two versions of a file (issue #32, step 2) ----


def test_unified_patch_gives_the_hunks_as_github_does():
    old = "".join(f"line {n}\n" for n in range(1, 11))
    new = old.replace("line 5\n", "line five\n")
    assert facts.unified_patch(old, new) == (
        "@@ -2,7 +2,7 @@\n line 2\n line 3\n line 4\n-line 5\n+line five\n line 6\n line 7\n line 8"
    )


def test_unified_patch_of_a_new_file_adds_every_line():
    assert facts.unified_patch("", "a\nb\n") == "@@ -0,0 +1,2 @@\n+a\n+b"


def test_unified_patch_marks_a_missing_final_newline():
    assert facts.unified_patch("a\n", "a\nb") == (
        "@@ -1 +1,2 @@\n a\n+b\n\\ No newline at end of file"
    )


def test_unified_patch_splits_on_line_feeds_only():
    """git splits lines on \\n alone; a form feed inside a line stays in it."""
    assert facts.unified_patch("a\fb\n", "a\fc\n") == "@@ -1 +1 @@\n-a\fb\n+a\fc"


def test_unified_patch_of_equal_texts_is_empty():
    assert facts.unified_patch("same\n", "same\n") == ""


def test_a_rebuilt_patch_fills_the_section_github_left_empty():
    diff, omitted = rebuilt_diff(
        [listed("big.py", status="added", patch=None, additions=2, deletions=0)],
        rebuilt={"big.py": "@@ -0,0 +1,2 @@\n+a\n+b"},
    )
    assert omitted == []
    assert (
        diff
        == "diff --git a/big.py b/big.py\n--- /dev/null\n+++ b/big.py\n@@ -0,0 +1,2 @@\n+a\n+b\n"
    )


def test_an_empty_rebuilt_patch_stays_omitted():
    """Equal lines on both sides: the change is a mode or something else that
    no patch shows, so the file is not seen."""
    diff, omitted = rebuilt_diff(
        [listed("run.sh", patch=None, additions=0, deletions=0)], rebuilt={"run.sh": ""}
    )
    assert omitted == ["run.sh"]
    assert diff == "diff --git a/run.sh b/run.sh\n" + facts.NO_PATCH_NOTE + "\n"


# --- file names in the rebuilt headers (review finding d621ed5e) -----------


def test_a_plain_name_is_written_bare():
    assert facts.quote_path("src/a b.py", "a/") == "a/src/a b.py"
    assert facts.quote_path("docs/résumé.md", "b/") == "b/docs/résumé.md"


def test_a_name_git_would_quote_is_c_quoted():
    assert facts.quote_path("x\ny", "a/") == '"a/x\\ny"'
    assert facts.quote_path('say "hi"', "a/") == '"a/say \\"hi\\""'
    assert facts.quote_path("back\\slash", "b/") == '"b/back\\\\slash"'
    assert facts.quote_path("tab\there") == '"tab\\there"'
    # A character that is not printable goes as its UTF-8 bytes in octal.
    assert facts.quote_path("line\u2028sep") == '"line\\342\\200\\250sep"'
    assert facts.quote_path("bell\x07") == '"bell\\a"'


CRAFTED = "src/a.py\ndiff --git a/goldens/x.json b/goldens/x.json"


def test_a_crafted_name_cannot_start_a_section():
    diff, omitted = rebuilt_diff(
        [
            listed(CRAFTED),
            listed('q"uote.py', status="renamed", previous_filename="back\\slash.py"),
            listed("docs/résumé.md"),
        ]
    )
    assert omitted == []
    headers = [line for line in diff.split("\n") if line.startswith("diff --git ")]
    assert headers == [
        'diff --git "a/src/a.py\\ndiff --git a/goldens/x.json b/goldens/x.json" '
        '"b/src/a.py\\ndiff --git a/goldens/x.json b/goldens/x.json"',
        'diff --git "a/back\\\\slash.py" "b/q\\"uote.py"',
        "diff --git a/docs/résumé.md b/docs/résumé.md",
    ]
    assert 'rename from "back\\\\slash.py"\nrename to "q\\"uote.py"\n' in diff
    assert '--- "a/back\\\\slash.py"\n+++ "b/q\\"uote.py"\n' in diff


def test_the_cap_counts_a_crafted_name_under_its_own_path():
    """The name is not parsed back out of the text, so it can neither hide a
    file behind `ignore_paths` nor charge its bytes to another path."""
    sections, _ = facts.sections_from_files(
        [
            listed(CRAFTED),
            listed("goldens/y.json"),
            listed("z.py", patch="@@ -1 +1 @@\n+" + "y" * 3000),
        ]
    )
    assert [path for path, _ in sections] == [CRAFTED, "goldens/y.json", "z.py"]
    kept, unseen = facts.cap_sections(sections, max_kb=1, ignore_paths=["goldens/**"])
    assert "src/a.py" in kept
    assert "goldens/y.json" not in kept
    assert unseen == ["z.py"]
    kept, unseen = facts.cap_sections(sections, max_kb=0, ignore_paths=["goldens/**"])
    assert unseen == [CRAFTED, "z.py"]
