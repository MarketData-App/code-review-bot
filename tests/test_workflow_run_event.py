"""A review can be triggered by CI finishing, not by the push that starts it.

`pull_request_target` fires on `opened` and `synchronize` — the moment CI
STARTS. Measured on sdk-py: its test workflow takes about 75 seconds, and a
review reaches the gate in 30-40, so it finds CI `pending`, skips, and nothing
ever re-triggers. Arming the standard channel without this would mean automatic
reviews that never happen and runs that report success.

So the caller triggers on `workflow_run` instead: CI completes, then the review
runs against a commit whose checks are final.

Run: pytest tests/test_workflow_run_event.py
"""

import pytest

from reviewbot import cli, github
from tests.conftest import FakeTransport

TOKEN = "ghs_" + "T" * 36
REPO = "MarketDataApp/sdk-py"


def wf_run(conclusion="success", prs=None, head_sha="a" * 40):
    return {
        "workflow_run": {
            "conclusion": conclusion,
            "head_sha": head_sha,
            "head_branch": "feat/x",
            "pull_requests": prs if prs is not None else [{"number": 42}],
        }
    }


def test_the_pull_request_comes_from_the_payload_when_it_is_there():
    assert cli.pr_number_from_event(wf_run()) == 42


def test_a_workflow_run_with_no_pull_requests_needs_a_lookup():
    # GitHub leaves `pull_requests` empty for a fork's pull request, so the
    # payload alone cannot answer. It must not guess.
    assert cli.pr_number_from_event(wf_run(prs=[])) is None


def test_the_sha_lookup_finds_it(api_for_repo):
    api, transport = api_for_repo
    transport.add(
        "GET",
        f"/repos/{REPO}/commits/{'a' * 40}/pulls",
        data=[{"number": 77, "state": "open"}],
    )
    assert api.pull_for_sha("a" * 40) == 77


def test_the_sha_lookup_ignores_a_closed_pull_request(api_for_repo):
    api, transport = api_for_repo
    transport.add(
        "GET",
        f"/repos/{REPO}/commits/{'a' * 40}/pulls",
        data=[{"number": 77, "state": "closed"}],
    )
    assert api.pull_for_sha("a" * 40) is None


def test_the_sha_lookup_is_not_fatal_when_it_fails(api_for_repo):
    api, transport = api_for_repo
    transport.add("GET", f"/repos/{REPO}/commits/{'a' * 40}/pulls", status=500, text="boom")
    assert api.pull_for_sha("a" * 40) is None


@pytest.fixture
def api_for_repo():
    transport = FakeTransport()
    return github.GitHub(REPO, TOKEN, transport=transport, sleep=lambda s: None), transport
