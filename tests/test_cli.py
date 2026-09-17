"""Tests for the order of operations.

The CLI is driven with a fake GitHub and a fake `claude` on PATH, so the whole
run is exercised without a network or a model.

Run: pytest tests/test_cli.py
"""

import json

import pytest

from reviewbot import cli, markers
from reviewbot.facts import PRFacts
from tests.conftest import VALID_RESULT, claude_script, write_script

VALID_JSON = json.dumps(VALID_RESULT)


class FakeGitHub:
    """Records every write. Answers reads from the attributes set on it."""

    def __init__(self, pr: PRFacts, files=None, repo_settings=None):
        self.pr = pr
        self.files = files or {}
        self.repo_settings = (
            repo_settings if repo_settings is not None else {"allow_auto_merge": True}
        )
        self.comments = []
        self.checks = []
        self.labels = []
        self.approvals = []
        self.auto_merge = []

    def file_at_ref(self, path, ref):
        return self.files.get(path)

    def repository(self):
        return self.repo_settings

    def is_org_member(self, org, login):
        # The workflow gives `run` the same org client the gate uses. These
        # fixtures predate that, so they answer "cannot tell" and exercise the
        # author_association fallback, which is what MEMBER in make_pr means.
        return None

    def gather(self, number, max_diff_kb, check_name="Code review", ignore_paths=None):
        self.gather_ignore_paths = ignore_paths
        return self.pr

    def upsert_review_comment(self, number, body):
        self.comments.append(body)
        return {"id": 1}

    def write_check_run(self, sha, name, conclusion, title, summary, annotations):
        self.checks.append(
            {
                "sha": sha,
                "name": name,
                "conclusion": conclusion,
                "title": title,
                "summary": summary,
                "annotations": annotations,
            }
        )
        return {"id": 2}

    def apply_labels(self, number, add, remove, current):
        self.labels.append({"add": add, "remove": remove, "current": current})
        return add

    def approve(self, number, body):
        self.approvals.append(body)
        return {"id": 3}

    def set_auto_merge(self, node_id, method):
        self.auto_merge.append(("arm", node_id, method))

    def clear_auto_merge(self, node_id):
        self.auto_merge.append(("disarm", node_id))


def make_pr(**over):
    base = dict(
        number=7,
        title="Add a retry",
        body="Because of 503s.",
        author="alice",
        author_is_bot=False,
        draft=False,
        labels=[],
        head_sha="a" * 40,
        base_ref="main",
        node_id="PR_node",
        author_association="MEMBER",
        changed_files=[
            {"path": "sdk/client.py", "status": "modified", "additions": 3, "deletions": 1}
        ],
        diff="diff --git a/sdk/client.py b/sdk/client.py\n+ retry()\n",
        unseen_files=[],
        ci_state="success",
        previous_comment=None,
        previous_state={},
        comments_since=[],
    )
    base.update(over)
    return PRFacts(**base)


EVENT = {"action": "synchronize", "pull_request": {"number": 7}}


@pytest.fixture
def live_claude(bin_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token-value")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    write_script(bin_dir, "claude", claude_script(VALID_JSON))
    return tmp_path


def review(api, event=None, checkout="/w/pr", force=False):
    return cli.run(
        event=event or EVENT,
        repo="MarketData-App/api",
        token="ghs_x",
        checkout=checkout,
        api=api,
        force=force,
    )


# --- event routing ---------------------------------------------------------


def test_a_pull_request_event_gives_the_number():
    assert cli.pr_number_from_event(EVENT) == 7


def test_an_issue_comment_on_a_pr_gives_the_number():
    event = {
        "action": "created",
        "issue": {"number": 12, "pull_request": {"url": "u"}},
        "comment": {"body": "@marketdata-code-review re-review"},
    }
    assert cli.pr_number_from_event(event) == 12


def test_an_issue_comment_on_an_issue_is_ignored():
    event = {
        "action": "created",
        "issue": {"number": 12},
        "comment": {"body": "@marketdata-code-review re-review"},
    }
    assert cli.pr_number_from_event(event) is None


def test_an_unrelated_pr_comment_is_ignored():
    event = {
        "action": "created",
        "issue": {"number": 12, "pull_request": {"url": "u"}},
        "comment": {"body": "looks good"},
    }
    assert cli.pr_number_from_event(event) is None


def test_a_workflow_dispatch_gives_the_number():
    assert cli.pr_number_from_event({"inputs": {"pr": "31"}}) == 31


def test_an_ignored_comment_event_writes_nothing(live_claude):
    api = FakeGitHub(make_pr())
    event = {
        "action": "created",
        "issue": {"number": 7, "pull_request": {"url": "u"}},
        "comment": {"body": "nice work"},
    }
    assert review(api, event) == 0
    assert (api.comments, api.checks, api.labels) == ([], [], [])


# --- skips -----------------------------------------------------------------


def test_a_draft_gets_no_comment_and_no_check(live_claude):
    api = FakeGitHub(make_pr(draft=True))
    assert review(api) == 0
    assert api.comments == []
    assert api.checks == []


def test_an_ignored_author_gets_no_comment(live_claude):
    api = FakeGitHub(make_pr(author="dependabot[bot]"))
    assert review(api) == 0
    assert api.comments == []


# --- the happy path --------------------------------------------------------


def test_a_review_writes_the_comment_the_check_and_the_labels(live_claude):
    api = FakeGitHub(make_pr())
    assert review(api) == 0
    assert len(api.comments) == 1
    assert markers.MARKER in api.comments[0]
    assert api.checks[0]["name"] == "Code review"
    assert api.checks[0]["sha"] == "a" * 40
    assert api.labels[0]["add"] == ["review: needs changes"]


def test_located_findings_become_annotations(live_claude):
    api = FakeGitHub(make_pr())
    review(api)
    annotation = api.checks[0]["annotations"][0]
    assert annotation["path"] == "sdk/client.py"
    assert annotation["start_line"] == 42
    assert annotation["annotation_level"] == "warning"  # should_fix
    assert "Retry loop never sleeps" in annotation["title"]


def test_the_annotation_level_follows_the_severity():
    findings = [
        {
            "file": "a.py",
            "line_start": 1,
            "line_end": None,
            "severity": "blocking",
            "title": "t",
            "body": "b",
            "category": "correctness",
            "confidence": 1.0,
            "id": "1",
        },
        {
            "file": "b.py",
            "line_start": 2,
            "line_end": 4,
            "severity": "nit",
            "title": "t",
            "body": "b",
            "category": "style",
            "confidence": 1.0,
            "id": "2",
        },
        {
            "file": None,
            "line_start": None,
            "line_end": None,
            "severity": "blocking",
            "title": "t",
            "body": "b",
            "category": "docs",
            "confidence": 1.0,
            "id": "3",
        },
    ]
    out = cli.annotations_for(findings)
    assert [a["annotation_level"] for a in out] == ["failure", "notice"]
    assert out[1]["end_line"] == 4


def test_the_policy_comes_from_the_base_branch(live_claude):
    api = FakeGitHub(make_pr(), files={".github/code-review/policy.yml": "check_name: Review\n"})
    review(api)
    assert api.checks[0]["name"] == "Review"


def test_the_repo_review_md_reaches_the_brief(live_claude, bin_dir, tmp_path):
    seen = tmp_path / "seen.json"
    api = FakeGitHub(make_pr(), files={".github/code-review/REVIEW.md": "HOUSE RULE ONE"})
    # The fake claude records the brief it was given.
    write_script(bin_dir, "claude", claude_script(VALID_JSON, echo_args=str(seen)))
    review(api)
    assert "HOUSE RULE ONE" in json.loads(seen.read_text())["brief"]


def test_an_unknown_include_fails_the_run_with_a_neutral_check(live_claude):
    """A typo in an include used to be invisible.

    The old loader read only the first line, so `@include defualt` was passed
    to the model as literal text: the default rules silently vanished and the
    review came back thin with nothing to say why.
    """
    api = FakeGitHub(
        make_pr(),
        files={".github/code-review/REVIEW.md": "@include defualt\n\nHouse rules.\n"},
    )
    assert review(api) == 1
    assert api.checks[0]["conclusion"] == "neutral"
    assert "defualt" in api.checks[0]["summary"]
    assert api.comments == []


def test_the_rule_sets_a_repo_asked_for_reach_the_brief_and_the_footer(
    live_claude, bin_dir, tmp_path
):
    seen = tmp_path / "seen.json"
    api = FakeGitHub(
        make_pr(),
        files={".github/code-review/REVIEW.md": "@include default\n\nHOUSE RULE ONE"},
    )
    write_script(bin_dir, "claude", claude_script(VALID_JSON, echo_args=str(seen)))
    review(api)
    brief_text = json.loads(seen.read_text())["brief"]
    assert "Standing review instructions" in brief_text
    assert "HOUSE RULE ONE" in brief_text
    assert "@include" not in brief_text
    assert "rules: default" in api.comments[0]


def test_a_broken_policy_fails_the_run_with_a_neutral_check(live_claude):
    api = FakeGitHub(make_pr(), files={".github/code-review/policy.yml": "mode: sometimes\n"})
    assert review(api) == 1
    assert api.checks[0]["conclusion"] == "neutral"
    assert "sometimes" in api.checks[0]["summary"]
    assert api.comments == []


# --- the loop --------------------------------------------------------------


def test_the_first_review_is_revision_one(live_claude):
    api = FakeGitHub(make_pr())
    review(api)
    assert markers.parse(api.comments[0])["revision"] == 1


def test_a_rerun_on_the_same_sha_keeps_the_revision(live_claude):
    # Reachable via --force and via a bot command; an ordinary re-run now
    # skips instead, which the test below pins.
    state = {"reviewed_sha": "a" * 40, "revision": 3, "finding_ids": []}
    api = FakeGitHub(make_pr(previous_state=state, previous_comment={"id": 9, "body": "x"}))
    review(api, force=True)
    assert markers.parse(api.comments[0])["revision"] == 3


def test_an_unchanged_head_is_not_reviewed_again(live_claude):
    """The model must not run, and the comment must not be rewritten."""
    state = {"reviewed_sha": "a" * 40, "revision": 3, "finding_ids": []}
    api = FakeGitHub(make_pr(previous_state=state, previous_comment={"id": 9, "body": "x"}))
    assert review(api) == 0
    assert api.comments == []


def test_a_new_sha_increments_the_revision(live_claude):
    state = {"reviewed_sha": "b" * 40, "revision": 3, "finding_ids": []}
    api = FakeGitHub(make_pr(previous_state=state, previous_comment={"id": 9, "body": "x"}))
    review(api)
    assert markers.parse(api.comments[0])["revision"] == 4


def test_a_resolved_finding_is_reported(live_claude):
    state = {"reviewed_sha": "b" * 40, "revision": 1, "finding_ids": ["deadbeef"]}
    api = FakeGitHub(make_pr(previous_state=state, previous_comment={"id": 9, "body": "x"}))
    review(api)
    assert "deadbeef" in api.comments[0]


def test_a_maintainer_waiver_in_a_comment_is_honoured(live_claude):
    from reviewbot import findings as findings_mod

    waived_id = findings_mod.finding_id(VALID_RESULT["findings"][0])
    api = FakeGitHub(
        make_pr(
            comments_since=[
                {
                    "author": "selden",
                    "body": f"@marketdata-code-review waive {waived_id}",
                    "is_maintainer": True,
                }
            ]
        )
    )
    review(api)
    assert "waived by selden" in api.comments[0]
    assert markers.parse(api.comments[0])["waived"] == {waived_id: "selden"}


# --- approval and auto-merge ----------------------------------------------


def test_no_approval_and_no_auto_merge_by_default(live_claude):
    api = FakeGitHub(make_pr())
    review(api)
    assert api.approvals == []
    assert api.auto_merge == []


def test_approval_happens_when_policy_and_verdict_allow(live_claude, bin_dir):
    ready = json.loads(VALID_JSON)
    ready["findings"] = []
    ready["verdict"] = {"value": "ready", "reason": "Clean."}
    write_script(bin_dir, "claude", claude_script(json.dumps(ready)))
    api = FakeGitHub(make_pr(), files={".github/code-review/policy.yml": "auto_approve: true\n"})
    review(api)
    assert len(api.approvals) == 1


def test_auto_merge_is_armed_when_every_condition_holds(live_claude, bin_dir):
    ready = json.loads(VALID_JSON)
    ready["findings"] = []
    ready["verdict"] = {"value": "ready", "reason": "Clean."}
    write_script(bin_dir, "claude", claude_script(json.dumps(ready)))
    api = FakeGitHub(
        make_pr(),
        files={
            ".github/code-review/policy.yml": "auto_merge:\n  enabled: true\n  authors: [alice]\n"
        },
    )
    review(api)
    assert api.auto_merge == [("arm", "PR_node", "squash")]


def test_auto_merge_is_disarmed_when_a_condition_fails(live_claude):
    api = FakeGitHub(
        make_pr(),
        files={
            ".github/code-review/policy.yml": "auto_merge:\n  enabled: true\n  authors: [alice]\n"
        },
    )
    review(api)
    assert api.auto_merge == [("disarm", "PR_node")]


def test_auto_merge_says_so_when_the_repository_forbids_it(live_claude, bin_dir):
    ready = json.loads(VALID_JSON)
    ready["findings"] = []
    ready["verdict"] = {"value": "ready", "reason": "Clean."}
    write_script(bin_dir, "claude", claude_script(json.dumps(ready)))
    api = FakeGitHub(
        make_pr(),
        files={
            ".github/code-review/policy.yml": "auto_merge:\n  enabled: true\n  authors: [alice]\n"
        },
        repo_settings={"allow_auto_merge": False},
    )
    assert review(api) == 0
    assert api.auto_merge == []  # never even attempted
    assert "Allow auto-merge" in api.comments[0]


def test_an_unknown_allow_auto_merge_field_still_arms(live_claude, bin_dir):
    ready = json.loads(VALID_JSON)
    ready["findings"] = []
    ready["verdict"] = {"value": "ready", "reason": "Clean."}
    write_script(bin_dir, "claude", claude_script(json.dumps(ready)))
    api = FakeGitHub(
        make_pr(),
        files={
            ".github/code-review/policy.yml": "auto_merge:\n  enabled: true\n  authors: [alice]\n"
        },
        repo_settings={},
    )
    review(api)
    assert api.auto_merge == [("arm", "PR_node", "squash")]


def test_an_auto_merge_error_does_not_fail_the_review(live_claude, bin_dir):
    from reviewbot.github import GitHubError

    ready = json.loads(VALID_JSON)
    ready["findings"] = []
    ready["verdict"] = {"value": "ready", "reason": "Clean."}
    write_script(bin_dir, "claude", claude_script(json.dumps(ready)))

    api = FakeGitHub(
        make_pr(),
        files={
            ".github/code-review/policy.yml": "auto_merge:\n  enabled: true\n  authors: [alice]\n"
        },
    )

    def boom(node_id, method):
        raise GitHubError("Allow auto-merge is disabled for this repository")

    api.set_auto_merge = boom
    assert review(api) == 0
    assert "Allow auto-merge is disabled" in api.comments[0]


# --- failures --------------------------------------------------------------


def test_no_live_backend_writes_a_neutral_check_and_fails(bin_dir, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    api = FakeGitHub(make_pr())
    assert review(api) == 1
    assert api.checks[0]["conclusion"] == "neutral"
    assert "no backend" in api.checks[0]["summary"]
    assert api.comments == []


def test_a_backend_failure_leaves_the_existing_comment_alone(bin_dir, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    write_script(bin_dir, "claude", claude_script(VALID_JSON, exit_code=1))
    api = FakeGitHub(make_pr(previous_comment={"id": 9, "body": "old review"}))
    assert review(api) == 1
    assert api.comments == []
    assert api.checks[0]["conclusion"] == "neutral"


def test_the_error_check_never_carries_the_token(bin_dir, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    script = (
        "import sys; sys.stdin.read(); sys.stderr.write('bad token ghs_"
        + "T" * 36
        + "'); sys.exit(1)"
    )
    write_script(bin_dir, "claude", script)
    api = FakeGitHub(make_pr())
    review(api)
    assert "ghs_" not in api.checks[0]["summary"]


def test_main_reads_the_event_file(tmp_path, monkeypatch, live_claude):
    event = tmp_path / "event.json"
    event.write_text(json.dumps(EVENT))
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_x")
    monkeypatch.setenv("GITHUB_REPOSITORY", "MarketData-App/api")
    calls = {}

    def fake_run(**kwargs):
        calls.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run", fake_run)
    assert cli.main(["run", "--event", str(event), "--checkout", "/w/pr"]) == 0
    assert calls["repo"] == "MarketData-App/api"
    assert calls["event"]["pull_request"]["number"] == 7


def test_main_fails_without_a_token(tmp_path, monkeypatch):
    event = tmp_path / "event.json"
    event.write_text(json.dumps(EVENT))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "MarketData-App/api")
    with pytest.raises(SystemExit):
        cli.main(["run", "--event", str(event), "--checkout", "/w/pr"])


# --- the org-only gate, end to end -----------------------------------------


def test_an_outsider_reaching_run_is_loud_not_silent(live_claude):
    # The gate refuses this before the head is fetched, so reaching `run` at
    # all means the caller is misconfigured. A policy skip is silent by
    # design; this must not look the same, or a missing REVIEWBOT_ORG_TOKEN
    # reads as "reviewed and skipped" with a green tick and nothing written.
    api = FakeGitHub(make_pr(author="mallory", author_association="CONTRIBUTOR"))
    assert review(api) == 1
    assert api.comments == []
    assert api.checks[0]["conclusion"] == "neutral"
    assert "REVIEWBOT_ORG_TOKEN" in api.checks[0]["summary"]


def test_the_trusted_author_env_var_admits_a_bot(live_claude, monkeypatch):
    monkeypatch.setenv("REVIEWBOT_TRUSTED_AUTHORS", "sdk-sync[bot], other[bot]")
    api = FakeGitHub(make_pr(author="sdk-sync[bot]", author_association="NONE"))
    assert review(api) == 0
    assert len(api.comments) == 1


def test_the_env_var_does_not_admit_an_unlisted_bot(live_claude, monkeypatch):
    monkeypatch.setenv("REVIEWBOT_TRUSTED_AUTHORS", "sdk-sync[bot]")
    api = FakeGitHub(make_pr(author="stranger[bot]", author_association="NONE"))
    assert review(api) == 1
    assert api.comments == []


def test_policy_trusted_authors_and_the_env_var_combine(live_claude, monkeypatch):
    monkeypatch.setenv("REVIEWBOT_TRUSTED_AUTHORS", "from-env[bot]")
    api = FakeGitHub(
        make_pr(author="from-policy[bot]", author_association="NONE"),
        files={".github/code-review/policy.yml": "trusted_authors: ['from-policy[bot]']\n"},
    )
    assert review(api) == 0
    assert len(api.comments) == 1


def test_the_ignore_paths_reach_the_diff_cap(live_claude):
    # Otherwise a large ignored fixture spends the whole diff budget and hides
    # the source beside it. ignore_paths used to be read only by the skip.
    api = FakeGitHub(
        make_pr(), files={".github/code-review/policy.yml": "ignore_paths: ['tests/fixtures/**']\n"}
    )
    review(api)
    assert api.gather_ignore_paths == ["tests/fixtures/**"]
