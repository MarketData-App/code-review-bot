"""Tests for the GitHub client.

The transport is injected, so no test reaches the network. The token appears
in exactly one place, the Authorization header, and these tests say so.

Run: pytest tests/test_github.py
"""

import json

import pytest

from reviewbot import github, markers

TOKEN = "ghs_" + "T" * 36


class FakeTransport:
    """Answers by (method, path). Records every call it was given."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def add(self, method, path, status=200, data=None, text=""):
        self.routes.setdefault((method, path), []).append(
            github.Response(status=status, data=data, text=text)
        )

    def __call__(self, method, url, headers, body):
        path = url.replace("https://api.github.com", "")
        self.calls.append(
            {
                "method": method,
                "path": path,
                "headers": headers,
                "body": json.loads(body) if body else None,
            }
        )
        queue = self.routes.get((method, path))
        if not queue:
            raise AssertionError(f"no fake route for {method} {path}")
        return queue.pop(0) if len(queue) > 1 else queue[0]


@pytest.fixture
def transport():
    return FakeTransport()


@pytest.fixture
def api(transport):
    return github.GitHub("MarketData-App/api", TOKEN, transport=transport, sleep=lambda s: None)


# --- transport basics ------------------------------------------------------


def test_the_token_travels_in_the_authorization_header_only(api, transport):
    transport.add("GET", "/repos/MarketData-App/api/pulls/7", data={"number": 7})
    api.pull_request(7)
    call = transport.calls[0]
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in call["path"]
    assert TOKEN not in json.dumps(call["body"])


def test_a_403_is_retried_then_succeeds(api, transport):
    transport.add("GET", "/repos/MarketData-App/api/pulls/7", status=403, text="rate limited")
    transport.add("GET", "/repos/MarketData-App/api/pulls/7", data={"number": 7})
    assert api.pull_request(7)["number"] == 7
    assert len(transport.calls) == 2


def test_a_500_is_retried(api, transport):
    transport.add("GET", "/repos/MarketData-App/api/pulls/7", status=500, text="oops")
    transport.add("GET", "/repos/MarketData-App/api/pulls/7", data={"number": 7})
    assert api.pull_request(7)["number"] == 7


def test_retries_give_up_and_raise(api, transport):
    for _ in range(6):
        transport.add("GET", "/repos/MarketData-App/api/pulls/7", status=500, text="oops")
    with pytest.raises(github.GitHubError) as excinfo:
        api.pull_request(7)
    assert "500" in str(excinfo.value)


def test_a_404_is_not_retried(api, transport):
    transport.add("GET", "/repos/MarketData-App/api/pulls/7", status=404, text="missing")
    with pytest.raises(github.GitHubError):
        api.pull_request(7)
    assert len(transport.calls) == 1


def test_an_error_message_never_repeats_the_token(api, transport):
    transport.add(
        "GET", "/repos/MarketData-App/api/pulls/7", status=404, text=f"bad credentials {TOKEN}"
    )
    with pytest.raises(github.GitHubError) as excinfo:
        api.pull_request(7)
    assert TOKEN not in str(excinfo.value)


# --- reads -----------------------------------------------------------------


def test_changed_files_pages_until_the_page_is_short(api, transport):
    page_one = [
        {"filename": f"f{i}.py", "status": "modified", "additions": 1, "deletions": 0}
        for i in range(100)
    ]
    transport.add(
        "GET", "/repos/MarketData-App/api/pulls/7/files?per_page=100&page=1", data=page_one
    )
    transport.add(
        "GET",
        "/repos/MarketData-App/api/pulls/7/files?per_page=100&page=2",
        data=[{"filename": "last.py", "status": "added", "additions": 2, "deletions": 0}],
    )
    files = api.changed_files(7)
    assert len(files) == 101
    assert files[-1] == {"path": "last.py", "status": "added", "additions": 2, "deletions": 0}


def test_the_diff_is_fetched_with_the_diff_media_type(api, transport):
    transport.add("GET", "/repos/MarketData-App/api/pulls/7", text="diff --git a/x b/x\n")
    assert api.diff(7).startswith("diff --git")
    assert transport.calls[0]["headers"]["Accept"] == "application/vnd.github.v3.diff"


def test_the_repository_object_carries_allow_auto_merge(api, transport):
    transport.add("GET", "/repos/MarketData-App/api", data={"allow_auto_merge": False})
    assert api.repository()["allow_auto_merge"] is False


def test_ci_state_is_failure_when_any_check_failed(api, transport):
    transport.add(
        "GET",
        "/repos/MarketData-App/api/commits/abc/check-runs?per_page=100",
        data={"check_runs": [{"name": "Tests", "status": "completed", "conclusion": "failure"}]},
    )
    transport.add("GET", "/repos/MarketData-App/api/commits/abc/status", data={"state": "success"})
    assert api.ci_state("abc", exclude_check_name="Code review") == "failure"


def test_ci_state_ignores_the_bot_own_check(api, transport):
    transport.add(
        "GET",
        "/repos/MarketData-App/api/commits/abc/check-runs?per_page=100",
        data={
            "check_runs": [{"name": "Code review", "status": "completed", "conclusion": "failure"}]
        },
    )
    transport.add("GET", "/repos/MarketData-App/api/commits/abc/status", data={"state": "pending"})
    assert api.ci_state("abc", exclude_check_name="Code review") == "none"


def test_ci_state_is_pending_while_a_check_runs(api, transport):
    transport.add(
        "GET",
        "/repos/MarketData-App/api/commits/abc/check-runs?per_page=100",
        data={"check_runs": [{"name": "Tests", "status": "in_progress", "conclusion": None}]},
    )
    transport.add("GET", "/repos/MarketData-App/api/commits/abc/status", data={"state": "pending"})
    assert api.ci_state("abc", exclude_check_name="Code review") == "pending"


def test_ci_state_is_success_when_everything_passed(api, transport):
    transport.add(
        "GET",
        "/repos/MarketData-App/api/commits/abc/check-runs?per_page=100",
        data={"check_runs": [{"name": "Tests", "status": "completed", "conclusion": "success"}]},
    )
    transport.add("GET", "/repos/MarketData-App/api/commits/abc/status", data={"state": "success"})
    assert api.ci_state("abc", exclude_check_name="Code review") == "success"


def test_ci_state_is_none_without_any_check(api, transport):
    transport.add(
        "GET",
        "/repos/MarketData-App/api/commits/abc/check-runs?per_page=100",
        data={"check_runs": []},
    )
    transport.add("GET", "/repos/MarketData-App/api/commits/abc/status", data={"state": "pending"})
    assert api.ci_state("abc", exclude_check_name="Code review") == "none"


# --- the comment -----------------------------------------------------------


def test_the_first_review_posts_a_new_comment(api, transport):
    transport.add(
        "GET",
        "/repos/MarketData-App/api/issues/7/comments?per_page=100&page=1",
        data=[
            {
                "id": 1,
                "body": "a human comment",
                "user": {"login": "alice"},
                "author_association": "CONTRIBUTOR",
                "created_at": "2026-09-01T00:00:00Z",
                "updated_at": "2026-09-01T00:00:00Z",
            }
        ],
    )
    transport.add("POST", "/repos/MarketData-App/api/issues/7/comments", status=201, data={"id": 2})
    api.upsert_review_comment(7, "BODY " + markers.MARKER)
    assert transport.calls[-1]["method"] == "POST"
    assert transport.calls[-1]["body"]["body"].startswith("BODY")


def test_a_second_review_edits_the_same_comment(api, transport):
    transport.add(
        "GET",
        "/repos/MarketData-App/api/issues/7/comments?per_page=100&page=1",
        data=[
            {
                "id": 9,
                "body": "old review " + markers.MARKER,
                "user": {"login": "marketdata-code-review[bot]"},
                "author_association": "NONE",
                "created_at": "2026-09-01T00:00:00Z",
                "updated_at": "2026-09-01T00:00:00Z",
            }
        ],
    )
    transport.add("PATCH", "/repos/MarketData-App/api/issues/comments/9", data={"id": 9})
    api.upsert_review_comment(7, "NEW BODY " + markers.MARKER)
    assert transport.calls[-1]["method"] == "PATCH"


# --- the check run ---------------------------------------------------------


def test_the_check_run_carries_the_conclusion_and_the_annotations(api, transport):
    transport.add("POST", "/repos/MarketData-App/api/check-runs", status=201, data={"id": 5})
    api.write_check_run(
        "abc",
        "Code review",
        "failure",
        "⛔ Blocked",
        "summary text",
        [
            {
                "path": "a.py",
                "start_line": 1,
                "end_line": 1,
                "annotation_level": "failure",
                "message": "m",
                "title": "t",
            }
        ],
    )
    body = transport.calls[-1]["body"]
    assert body["head_sha"] == "abc"
    assert body["conclusion"] == "failure"
    assert body["status"] == "completed"
    assert body["output"]["annotations"][0]["path"] == "a.py"


def test_annotations_beyond_fifty_go_in_a_second_call(api, transport):
    transport.add("POST", "/repos/MarketData-App/api/check-runs", status=201, data={"id": 5})
    transport.add("PATCH", "/repos/MarketData-App/api/check-runs/5", data={"id": 5})
    annotations = [
        {
            "path": f"f{i}.py",
            "start_line": 1,
            "end_line": 1,
            "annotation_level": "warning",
            "message": "m",
            "title": "t",
        }
        for i in range(60)
    ]
    api.write_check_run("abc", "Code review", "neutral", "t", "s", annotations)
    assert len(transport.calls[-2]["body"]["output"]["annotations"]) == 50
    assert len(transport.calls[-1]["body"]["output"]["annotations"]) == 10


# --- labels, approval, auto-merge ------------------------------------------


def test_labels_are_added_and_only_present_ones_removed(api, transport):
    transport.add("POST", "/repos/MarketData-App/api/issues/7/labels", data=[])
    # urllib.parse.quote escapes the colon too, so the path is fully encoded.
    transport.add(
        "DELETE",
        "/repos/MarketData-App/api/issues/7/labels/review%3A%20needs%20changes",
        status=200,
        data=[],
    )
    api.apply_labels(
        7,
        add=["review: ready"],
        remove=["review: needs changes", "review: ready"],
        current=["review: needs changes", "enhancement"],
    )
    methods = [c["method"] for c in transport.calls]
    assert methods.count("DELETE") == 1
    assert transport.calls[0]["body"]["labels"] == ["review: ready"]


def test_nothing_is_called_when_the_labels_already_match(api, transport):
    api.apply_labels(
        7, add=["review: ready"], remove=["review: needs changes"], current=["review: ready"]
    )
    assert transport.calls == []


def test_approval_posts_a_review(api, transport):
    transport.add("POST", "/repos/MarketData-App/api/pulls/7/reviews", status=200, data={"id": 1})
    api.approve(7, "Approved by the code review bot.")
    assert transport.calls[-1]["body"]["event"] == "APPROVE"


def test_auto_merge_is_armed_through_graphql(api, transport):
    transport.add("POST", "/graphql", data={"data": {"enablePullRequestAutoMerge": {}}})
    api.set_auto_merge("PR_node", "squash")
    body = transport.calls[-1]["body"]
    assert "enablePullRequestAutoMerge" in body["query"]
    assert body["variables"]["method"] == "SQUASH"


def test_auto_merge_is_disarmed_through_graphql(api, transport):
    transport.add("POST", "/graphql", data={"data": {"disablePullRequestAutoMerge": {}}})
    api.clear_auto_merge("PR_node")
    assert "disablePullRequestAutoMerge" in transport.calls[-1]["body"]["query"]


def test_a_graphql_error_raises(api, transport):
    transport.add("POST", "/graphql", data={"errors": [{"message": "auto-merge is not enabled"}]})
    with pytest.raises(github.GitHubError) as excinfo:
        api.set_auto_merge("PR_node", "squash")
    assert "auto-merge is not enabled" in str(excinfo.value)


# --- gather ----------------------------------------------------------------


def stock_pr_routes(transport, comments):
    transport.add(
        "GET",
        "/repos/MarketData-App/api/pulls/7",
        data={
            "number": 7,
            "title": "Add a retry",
            "body": "Because of 503s.",
            "user": {"login": "alice", "type": "User"},
            "draft": False,
            "labels": [{"name": "enhancement"}],
            "head": {"sha": "abc"},
            "base": {"ref": "main"},
            "node_id": "PR_node",
            "author_association": "MEMBER",
        },
    )
    transport.add(
        "GET",
        "/repos/MarketData-App/api/pulls/7/files?per_page=100&page=1",
        data=[{"filename": "sdk/client.py", "status": "modified", "additions": 3, "deletions": 1}],
    )
    transport.add(
        "GET",
        "/repos/MarketData-App/api/pulls/7",
        text="diff --git a/sdk/client.py b/sdk/client.py\n+ retry()\n",
    )
    transport.add(
        "GET", "/repos/MarketData-App/api/issues/7/comments?per_page=100&page=1", data=comments
    )
    transport.add(
        "GET",
        "/repos/MarketData-App/api/commits/abc/check-runs?per_page=100",
        data={"check_runs": [{"name": "Tests", "status": "completed", "conclusion": "success"}]},
    )
    transport.add("GET", "/repos/MarketData-App/api/commits/abc/status", data={"state": "success"})


def test_gather_builds_the_facts(api, transport):
    stock_pr_routes(transport, [])
    pr = api.gather(7, max_diff_kb=400)
    assert pr.number == 7
    assert pr.title == "Add a retry"
    assert pr.author == "alice"
    assert pr.author_is_bot is False
    assert pr.labels == ["enhancement"]
    assert pr.head_sha == "abc"
    assert pr.base_ref == "main"
    assert pr.node_id == "PR_node"
    assert pr.paths == ["sdk/client.py"]
    assert "retry()" in pr.diff
    assert pr.ci_state == "success"
    assert pr.previous_comment is None
    assert pr.previous_state == {}
    assert pr.comments_since == []


def test_gather_finds_the_previous_review_and_its_state(api, transport):
    state = markers.emit({"reviewed_sha": "old", "revision": 2, "finding_ids": ["1234abcd"]})
    stock_pr_routes(
        transport,
        [
            {
                "id": 9,
                "body": "old review\n" + state,
                "user": {"login": "marketdata-code-review[bot]"},
                "author_association": "NONE",
                "created_at": "2026-09-01T00:00:00Z",
                "updated_at": "2026-09-02T00:00:00Z",
            },
        ],
    )
    pr = api.gather(7, max_diff_kb=400)
    assert pr.previous_comment["id"] == 9
    assert pr.previous_state["revision"] == 2


def test_gather_collects_only_the_comments_after_the_last_review(api, transport):
    state = markers.emit({"reviewed_sha": "old", "revision": 1})
    stock_pr_routes(
        transport,
        [
            {
                "id": 1,
                "body": "before",
                "user": {"login": "alice"},
                "author_association": "CONTRIBUTOR",
                "created_at": "2026-09-01T00:00:00Z",
                "updated_at": "2026-09-01T00:00:00Z",
            },
            {
                "id": 9,
                "body": "review\n" + state,
                "user": {"login": "marketdata-code-review[bot]"},
                "author_association": "NONE",
                "created_at": "2026-09-02T00:00:00Z",
                "updated_at": "2026-09-02T00:00:00Z",
            },
            {
                "id": 10,
                "body": "after",
                "user": {"login": "selden"},
                "author_association": "OWNER",
                "created_at": "2026-09-03T00:00:00Z",
                "updated_at": "2026-09-03T00:00:00Z",
            },
        ],
    )
    pr = api.gather(7, max_diff_kb=400)
    assert [c["body"] for c in pr.comments_since] == ["after"]
    assert pr.comments_since[0]["is_maintainer"] is True


def test_gather_marks_a_bot_author(api, transport):
    transport.add(
        "GET",
        "/repos/MarketData-App/api/pulls/7",
        data={
            "number": 7,
            "title": "t",
            "body": "",
            "user": {"login": "sdk-bot[bot]", "type": "Bot"},
            "draft": False,
            "labels": [],
            "head": {"sha": "abc"},
            "base": {"ref": "main"},
            "node_id": "N",
        },
    )
    transport.add("GET", "/repos/MarketData-App/api/pulls/7/files?per_page=100&page=1", data=[])
    transport.add("GET", "/repos/MarketData-App/api/pulls/7", text="")
    transport.add("GET", "/repos/MarketData-App/api/issues/7/comments?per_page=100&page=1", data=[])
    transport.add(
        "GET",
        "/repos/MarketData-App/api/commits/abc/check-runs?per_page=100",
        data={"check_runs": []},
    )
    transport.add("GET", "/repos/MarketData-App/api/commits/abc/status", data={"state": "pending"})
    assert api.gather(7, max_diff_kb=400).author_is_bot is True


def test_gather_caps_the_diff_and_names_the_unseen_files(api, transport):
    transport.add(
        "GET",
        "/repos/MarketData-App/api/pulls/7",
        data={
            "number": 7,
            "title": "t",
            "body": "",
            "user": {"login": "alice", "type": "User"},
            "draft": False,
            "labels": [],
            "head": {"sha": "abc"},
            "base": {"ref": "main"},
            "node_id": "N",
        },
    )
    transport.add("GET", "/repos/MarketData-App/api/pulls/7/files?per_page=100&page=1", data=[])
    big = (
        "diff --git a/a.py b/a.py\n@@\n+small\ndiff --git a/b.py b/b.py\n@@\n+" + "y" * 3000 + "\n"
    )
    transport.add("GET", "/repos/MarketData-App/api/pulls/7", text=big)
    transport.add("GET", "/repos/MarketData-App/api/issues/7/comments?per_page=100&page=1", data=[])
    transport.add(
        "GET",
        "/repos/MarketData-App/api/commits/abc/check-runs?per_page=100",
        data={"check_runs": []},
    )
    transport.add("GET", "/repos/MarketData-App/api/commits/abc/status", data={"state": "pending"})
    pr = api.gather(7, max_diff_kb=1)
    assert pr.unseen_files == ["b.py"]


def test_file_at_ref_decodes_the_content(api, transport):
    import base64

    encoded = base64.b64encode(b"mode: all\n").decode()
    transport.add(
        "GET",
        "/repos/MarketData-App/api/contents/.github/code-review/policy.yml?ref=main",
        data={"encoding": "base64", "content": encoded},
    )
    assert api.file_at_ref(".github/code-review/policy.yml", "main") == "mode: all\n"


def test_a_missing_file_reads_as_none(api, transport):
    transport.add(
        "GET",
        "/repos/MarketData-App/api/contents/.github/code-review/policy.yml?ref=main",
        status=404,
        text="Not Found",
    )
    assert api.file_at_ref(".github/code-review/policy.yml", "main") is None


def test_ci_state_survives_no_access_to_the_legacy_status_api(api, transport):
    # The App has `checks` but not `statuses`. The combined-status endpoint is
    # an optional signal: most CI, Actions included, reports as check runs.
    # The bot must never fail the whole review because it cannot read it.
    transport.add(
        "GET",
        "/repos/MarketData-App/api/commits/abc/check-runs?per_page=100",
        data={"check_runs": [{"name": "Tests", "status": "completed", "conclusion": "success"}]},
    )
    transport.add(
        "GET",
        "/repos/MarketData-App/api/commits/abc/status",
        status=403,
        text='{"message":"Resource not accessible by integration"}',
    )
    assert api.ci_state("abc", exclude_check_name="Code review") == "success"


def test_a_failing_check_still_wins_without_the_legacy_api(api, transport):
    transport.add(
        "GET",
        "/repos/MarketData-App/api/commits/abc/check-runs?per_page=100",
        data={"check_runs": [{"name": "Tests", "status": "completed", "conclusion": "failure"}]},
    )
    transport.add("GET", "/repos/MarketData-App/api/commits/abc/status", status=403, text="nope")
    assert api.ci_state("abc", exclude_check_name="Code review") == "failure"


def test_no_checks_and_no_legacy_api_is_unknown(api, transport):
    transport.add(
        "GET",
        "/repos/MarketData-App/api/commits/abc/check-runs?per_page=100",
        data={"check_runs": []},
    )
    transport.add("GET", "/repos/MarketData-App/api/commits/abc/status", status=403, text="nope")
    # Not "success": we genuinely do not know, and require_ci_green must not
    # be satisfied by an absence of information.
    assert api.ci_state("abc", exclude_check_name="Code review") == "none"


def test_gather_carries_the_author_association(api, transport):
    # The org-only gate reads this. GitHub sets it; the pull request cannot.
    stock_pr_routes(transport, [])
    assert api.gather(7, max_diff_kb=400).author_association == "MEMBER"


# --- organisation membership -----------------------------------------------


def test_an_org_member_reads_as_true(api, transport):
    transport.add("GET", "/orgs/MarketData-App/members/MarketDataDev01", status=204)
    assert api.is_org_member("MarketData-App", "MarketDataDev01") is True


def test_a_non_member_reads_as_false(api, transport):
    transport.add("GET", "/orgs/MarketData-App/members/stranger", status=404, text="Not Found")
    assert api.is_org_member("MarketData-App", "stranger") is False


def test_no_permission_reads_as_unknown(api, transport):
    # Until the App is granted Organization members:read, this 403s. That is
    # "cannot tell", not "not a member": the caller falls back rather than
    # refusing everyone in the organisation.
    transport.add(
        "GET",
        "/orgs/MarketData-App/members/MarketDataDev01",
        status=403,
        text='{"message":"Resource not accessible by integration"}',
    )
    assert api.is_org_member("MarketData-App", "MarketDataDev01") is None


def test_a_server_error_reads_as_unknown(api, transport):
    for _ in range(6):
        transport.add("GET", "/orgs/MarketData-App/members/x", status=500, text="oops")
    assert api.is_org_member("MarketData-App", "x") is None


def test_an_empty_login_is_not_a_member(api, transport):
    assert api.is_org_member("MarketData-App", "") is False
