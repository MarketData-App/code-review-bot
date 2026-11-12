"""`reviewbot preflight`: decide before the self-hosted runner whether a CI
completion starts a review job (issue #27).

Each push to a pull request ends several CI workflows, and each one sends a
`workflow_run` to the caller. Under `review_on_push: first`, every one of them
after the first review must stop on a GitHub-hosted runner, not queue for the
one self-hosted runner that the private repositories share.

The command says "no" for that one reason only. Everything it cannot read, it
leaves to the review job, which decides as it always has.

Run: pytest tests/test_preflight.py
"""

import base64
import json

import pytest

from reviewbot import cli, github
from tests.conftest import FakeTransport

TOKEN = "ghs_" + "T" * 36
REPO = "MarketData-App/api"
HEAD = "b" * 40
FIRST = "review_on_push: first\n"

EVENT = {
    "workflow_run": {
        "conclusion": "success",
        "event": "pull_request",
        "head_sha": HEAD,
        "pull_requests": [{"number": 42}],
    }
}

POLICY_ROUTE = f"/repos/{REPO}/contents/.github/code-review/policy.yml?ref=main"
COMMENTS_ROUTE = f"/repos/{REPO}/issues/42/comments?per_page=100&page=1"


def bot_review(updated_at="2026-11-12T10:00:00Z"):
    return {
        "user": {"login": "marketdata-code-review[bot]", "type": "Bot"},
        "updated_at": updated_at,
        "created_at": "2026-11-12T09:00:00Z",
        "body": "review\n<!-- marketdata-code-review -->\n"
        f'<!-- reviewbot-state: {{"reviewed_sha":"{"a" * 40}","revision":1}} -->',
    }


def comment(body, created_at="2026-11-12T11:00:00Z"):
    return {
        "user": {"login": "alice", "type": "User"},
        "author_association": "MEMBER",
        "created_at": created_at,
        "body": body,
    }


def api_with(policy_text=None, comments=None, policy_status=200):
    transport = FakeTransport()
    transport.add(
        "GET",
        f"/repos/{REPO}/pulls/42",
        data={"number": 42, "base": {"ref": "main"}, "head": {"sha": HEAD}},
    )
    if policy_status != 200:
        transport.add("GET", POLICY_ROUTE, status=policy_status, text="boom")
    elif policy_text is None:
        transport.add("GET", POLICY_ROUTE, status=404, text="Not Found")
    else:
        transport.add(
            "GET",
            POLICY_ROUTE,
            data={
                "encoding": "base64",
                "content": base64.b64encode(policy_text.encode()).decode(),
            },
        )
    transport.add("GET", COMMENTS_ROUTE, data=comments or [])
    api = github.GitHub(REPO, TOKEN, transport=transport, sleep=lambda s: None)
    return api, transport


def decide(api, event=None):
    return cli.preflight(event=event or EVENT, repo=REPO, token=TOKEN, api=api)


def test_always_lets_every_push_through_without_reading_the_comments():
    api, transport = api_with(policy_text=None, comments=[bot_review()])
    assert decide(api)["review"] is True
    assert not any("/comments" in call["path"] for call in transport.calls)


def test_first_stops_a_push_to_a_reviewed_pull_request():
    api, _ = api_with(FIRST, comments=[bot_review()])
    decision = decide(api)
    assert decision["review"] is False
    assert "review_on_push is first" in decision["reason"]


def test_first_lets_the_first_review_start_by_itself():
    api, _ = api_with(FIRST, comments=[comment("looks big")])
    assert decide(api)["review"] is True


def test_first_lets_a_push_through_after_a_person_asked():
    api, _ = api_with(FIRST, comments=[bot_review(), comment("@marketdata-code-review re-review")])
    assert decide(api)["review"] is True


def test_a_request_older_than_the_last_review_was_already_served():
    asked_before = comment("@marketdata-code-review re-review", created_at="2026-11-12T09:30:00Z")
    api, _ = api_with(FIRST, comments=[asked_before, bot_review()])
    assert decide(api)["review"] is False


def test_a_malformed_policy_goes_to_the_review_job_which_reports_it():
    api, _ = api_with("review_on_push: sometimes\n")
    decision = decide(api)
    assert decision["review"] is True
    assert "review job" in decision["reason"]


def test_an_unreadable_pull_request_goes_to_the_review_job():
    transport = FakeTransport()
    transport.add("GET", f"/repos/{REPO}/pulls/42", status=500, text="boom")
    api = github.GitHub(REPO, TOKEN, transport=transport, sleep=lambda s: None)
    assert decide(api)["review"] is True


def test_an_event_with_no_pull_request_goes_to_the_review_job():
    """A fork's workflow_run carries no pull request and the sha lookup finds
    none. The review job already handles that, so preflight does not decide."""
    transport = FakeTransport()
    transport.add("GET", f"/repos/{REPO}/commits/{HEAD}/pulls", data=[])
    api = github.GitHub(REPO, TOKEN, transport=transport, sleep=lambda s: None)
    event = {"workflow_run": dict(EVENT["workflow_run"], pull_requests=[])}
    assert decide(api, event)["review"] is True


def test_the_token_never_reaches_the_reason():
    transport = FakeTransport()
    transport.add("GET", f"/repos/{REPO}/pulls/42", status=500, text=f"bad token {TOKEN}")
    api = github.GitHub(REPO, TOKEN, transport=transport, sleep=lambda s: None)
    assert TOKEN not in decide(api)["reason"]


@pytest.mark.parametrize(("review", "expected"), [(True, "review=true"), (False, "review=false")])
def test_the_command_writes_the_step_output(tmp_path, monkeypatch, review, expected):
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(EVENT))
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(cli, "preflight", lambda **kwargs: {"review": review, "reason": "because"})
    code = cli.main(["preflight", "--event", str(event_path), "--repo", REPO])
    assert code == 0
    assert out.read_text() == f"{expected}\n"
