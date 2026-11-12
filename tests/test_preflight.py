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


def test_a_marker_pasted_after_the_review_does_not_turn_reviews_back_on():
    pasted = comment(
        "quoting it\n<!-- marketdata-code-review -->\n"
        '<!-- reviewbot-state: {"reviewed_sha":"x","revision":1} -->'
    )
    api, _ = api_with(FIRST, comments=[bot_review(), pasted])
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


# --- another trigger workflow still runs on the head (issue #30) -------------
#
# The api caller starts on Tests, Lint and Schema. Lint and Schema end in well
# under a minute and Tests in about six, so each push sent three review jobs to
# the one self-hosted runner, and the two early ones only waited and skipped.
# Under `require_ci_green` an early completion now stops here while another of
# the CALLER's trigger workflows still runs on the head: that workflow's own
# completion is the next workflow_run, and it starts the review. Nothing else
# may stop a run, because nothing else sends a later workflow_run.

WORKFLOW_SHA = "c" * 40
WORKFLOW_REF = f"{REPO}/.github/workflows/code-review.yml@refs/heads/master"
CALLER_ROUTE = f"/repos/{REPO}/contents/.github/workflows/code-review.yml?ref={WORKFLOW_SHA}"
RUNS_ROUTE = f"/repos/{REPO}/actions/runs?head_sha={HEAD}&event=pull_request&per_page=100&page=1"
CHECK_RUNS_ROUTE = f"/repos/{REPO}/commits/{HEAD}/check-runs?per_page=100"

CALLER = """\
name: Code review
on:
  workflow_run:
    workflows: ["Tests", "Lint", "Schema"]
    types: [completed]
  issue_comment:
    types: [created]
jobs:
  review:
    uses: MarketData-App/code-review-bot/.github/workflows/review.yml@main
"""

RUN_IDS = {"Tests": 103, "Lint": 101, "Schema": 102, "Go Tests": 104}


def completion(name):
    """The workflow_run payload GitHub sends when `name` ends green."""
    return {"workflow_run": dict(EVENT["workflow_run"], id=RUN_IDS[name], name=name)}


def workflow_run(name, status="completed", run_id=None):
    return {
        "id": run_id or RUN_IDS[name],
        "name": name,
        "head_sha": HEAD,
        "event": "pull_request",
        "status": status,
        "conclusion": "success" if status == "completed" else None,
    }


def b64(text):
    return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}


def api_with_runs(runs, policy_text=None, comments=None, caller=CALLER):
    api, transport = api_with(policy_text, comments)
    if caller is None:
        transport.add("GET", CALLER_ROUTE, status=404, text="Not Found")
    else:
        transport.add("GET", CALLER_ROUTE, data=b64(caller))
    transport.add("GET", RUNS_ROUTE, data={"total_count": len(runs), "workflow_runs": runs})
    return api, transport


def decide_runs(api, event, workflow_ref=WORKFLOW_REF, workflow_sha=WORKFLOW_SHA):
    return cli.preflight(
        event=event,
        repo=REPO,
        token=TOKEN,
        api=api,
        workflow_ref=workflow_ref,
        workflow_sha=workflow_sha,
    )


def read(transport, route):
    return any(call["path"] == route for call in transport.calls)


def test_an_early_trigger_stops_while_another_trigger_still_runs():
    """api#743: Lint ends first while Tests still runs on the head."""
    runs = [workflow_run("Lint"), workflow_run("Schema"), workflow_run("Tests", "in_progress")]
    api, _ = api_with_runs(runs)
    decision = decide_runs(api, completion("Lint"))
    assert decision["review"] is False
    assert decision["reason"] == (
        f"#42: Tests is still running on {HEAD[:7]}; "
        "the last trigger workflow to finish starts the review"
    )


def test_every_trigger_still_running_is_named():
    runs = [
        workflow_run("Lint"),
        workflow_run("Schema", "queued"),
        workflow_run("Tests", "in_progress"),
    ]
    api, _ = api_with_runs(runs)
    decision = decide_runs(api, completion("Lint"))
    assert decision["review"] is False
    assert "Schema, Tests are still running" in decision["reason"]


def test_the_last_trigger_to_finish_starts_the_review():
    runs = [workflow_run("Lint"), workflow_run("Schema"), workflow_run("Tests")]
    api, _ = api_with_runs(runs)
    decision = decide_runs(api, completion("Tests"))
    assert decision["review"] is True
    assert "no other trigger workflow runs" in decision["reason"]


def test_another_apps_pending_check_does_not_stop_the_last_trigger():
    """Codecov reports a check run, not a workflow run, and sends no
    workflow_run. The preflight never reads check runs, so a pending Codecov
    check cannot stop the review; the review job's own wait handles it."""
    runs = [workflow_run("Lint"), workflow_run("Schema"), workflow_run("Tests")]
    api, transport = api_with_runs(runs)
    transport.add(
        "GET",
        CHECK_RUNS_ROUTE,
        data={"check_runs": [{"name": "codecov/patch", "status": "queued"}]},
    )
    assert decide_runs(api, completion("Tests"))["review"] is True
    assert not read(transport, CHECK_RUNS_ROUTE)


def test_a_workflow_that_is_not_a_trigger_does_not_stop_the_review():
    """api's Go Tests has a `paths:` filter and is not on the trigger list.
    Its completion starts no review, so waiting for it would lose this one."""
    runs = [
        workflow_run("Lint"),
        workflow_run("Schema"),
        workflow_run("Tests"),
        workflow_run("Go Tests", "in_progress"),
    ]
    api, _ = api_with_runs(runs)
    assert decide_runs(api, completion("Tests"))["review"] is True


def test_the_run_that_sent_the_event_never_stops_itself():
    """The listing can lag behind the completion it is answering."""
    runs = [workflow_run("Lint"), workflow_run("Schema"), workflow_run("Tests", "in_progress")]
    api, _ = api_with_runs(runs)
    assert decide_runs(api, completion("Tests"))["review"] is True


def test_a_rerun_of_a_trigger_on_the_same_head_still_starts_a_review():
    """A re-run keeps its run id and sends a new completion when it ends."""
    runs = [workflow_run("Lint"), workflow_run("Schema"), workflow_run("Tests")]
    api, _ = api_with_runs(runs)
    rerun = completion("Tests")
    rerun["workflow_run"]["run_attempt"] = 2
    assert decide_runs(api, rerun)["review"] is True


def test_a_second_run_of_the_same_trigger_still_running_stops_the_first():
    """Another pull_request activity on the same head starts a second Tests
    run. The first one's completion stops; the second one's starts the review."""
    runs = [
        workflow_run("Lint"),
        workflow_run("Schema"),
        workflow_run("Tests"),
        workflow_run("Tests", "in_progress", run_id=105),
    ]
    api, _ = api_with_runs(runs)
    assert decide_runs(api, completion("Tests"))["review"] is False


@pytest.mark.parametrize("status", ["requested", "queued", "pending", "waiting", "in_progress"])
def test_a_trigger_that_has_not_finished_stops_the_run(status):
    runs = [workflow_run("Lint"), workflow_run("Tests", status)]
    api, _ = api_with_runs(runs)
    assert decide_runs(api, completion("Lint"))["review"] is False


@pytest.mark.parametrize("status", ["completed", "action_required"])
def test_a_trigger_that_will_send_no_completion_soon_does_not_stop_the_run(status):
    """`action_required` waits for a person to approve the run, which may
    never happen, so it must not hold the review back."""
    runs = [workflow_run("Lint"), workflow_run("Tests", status)]
    api, _ = api_with_runs(runs)
    assert decide_runs(api, completion("Lint"))["review"] is True


def test_the_runs_are_read_for_the_event_that_started_this_run():
    """A trigger run from a `push` sends a workflow_run that the caller does
    not review, so only runs of the same event are read."""
    api, transport = api_with_runs([workflow_run("Lint")])
    decide_runs(api, completion("Lint"))
    assert read(transport, RUNS_ROUTE)


def test_the_trigger_list_is_read_from_the_callers_file_at_its_commit():
    api, transport = api_with_runs([workflow_run("Lint")])
    decide_runs(api, completion("Lint"))
    assert read(transport, CALLER_ROUTE)


def test_first_with_a_review_says_so_before_reading_any_run():
    api, transport = api_with_runs(
        [workflow_run("Tests", "in_progress")], FIRST, comments=[bot_review()]
    )
    decision = decide_runs(api, completion("Lint"))
    assert decision["review"] is False
    assert "review_on_push is first" in decision["reason"]
    assert not read(transport, RUNS_ROUTE)


def test_first_with_no_review_yet_still_waits_for_the_last_trigger():
    api, _ = api_with_runs([workflow_run("Lint"), workflow_run("Tests", "in_progress")], FIRST)
    assert decide_runs(api, completion("Lint"))["review"] is False


def test_without_require_ci_green_an_early_trigger_is_not_stopped():
    """Without require_ci_green the review job does not wait for CI, so the
    early review is a real one. If the later trigger then ended red or
    cancelled, it would send no green workflow_run, and the head would get no
    review at all."""
    api, transport = api_with_runs(
        [workflow_run("Lint"), workflow_run("Tests", "in_progress")],
        "require_ci_green: false\n",
    )
    decision = decide_runs(api, completion("Lint"))
    assert decision["review"] is True
    assert not read(transport, RUNS_ROUTE)
    assert not read(transport, CALLER_ROUTE)


# Every way the trigger list or the runs cannot be read gives review=true.


def test_an_unreadable_caller_file_goes_to_the_review_job():
    api, transport = api_with()
    transport.add("GET", CALLER_ROUTE, status=500, text=f"boom {TOKEN}")
    decision = decide_runs(api, completion("Lint"))
    assert decision["review"] is True
    assert "review job" in decision["reason"]
    assert TOKEN not in decision["reason"]


def test_unreadable_runs_go_to_the_review_job():
    """Without the App's `actions: read` the listing answers 403."""
    api, transport = api_with()
    transport.add("GET", CALLER_ROUTE, data=b64(CALLER))
    transport.add("GET", RUNS_ROUTE, status=403, text=f"Resource not accessible {TOKEN}")
    decision = decide_runs(api, completion("Lint"))
    assert decision["review"] is True
    assert "review job" in decision["reason"]
    assert TOKEN not in decision["reason"]


def test_a_missing_caller_file_reads_no_run():
    api, transport = api_with_runs([workflow_run("Tests", "in_progress")], caller=None)
    decision = decide_runs(api, completion("Lint"))
    assert decision["review"] is True
    assert "trigger workflows are unknown" in decision["reason"]
    assert not read(transport, RUNS_ROUTE)


@pytest.mark.parametrize(
    "caller",
    [
        "on: [: this is not yaml",
        "on:\n  pull_request_target:\n",
        "on:\n  workflow_run:\n    types: [completed]\n",
        "on:\n  workflow_run:\n    workflows: {Tests: x}\n",
        "on:\n  workflow_run:\n    workflows: []\n",
    ],
)
def test_a_caller_file_with_no_trigger_list_reads_no_run(caller):
    api, transport = api_with_runs([workflow_run("Tests", "in_progress")], caller=caller)
    assert decide_runs(api, completion("Lint"))["review"] is True
    assert not read(transport, RUNS_ROUTE)


def test_a_list_that_does_not_name_this_run_stops_nothing():
    """The list must describe the trigger that started this run, or it is not
    this caller's list."""
    caller = CALLER.replace('["Tests", "Lint", "Schema"]', '["Tests"]')
    api, transport = api_with_runs([workflow_run("Tests", "in_progress")], caller=caller)
    decision = decide_runs(api, completion("Lint"))
    assert decision["review"] is True
    assert "not in the caller's trigger list" in decision["reason"]
    assert not read(transport, RUNS_ROUTE)


@pytest.mark.parametrize(
    "workflow_ref",
    [
        "",
        "MarketData-App/code-review-bot/.github/workflows/review.yml@refs/heads/main",
        f"{REPO}-fork/.github/workflows/code-review.yml@refs/heads/master",
        f"{REPO}/docs/code-review.yml@refs/heads/master",
    ],
)
def test_a_workflow_ref_outside_the_callers_workflows_reads_nothing(workflow_ref):
    """Only the caller's own trigger list may stop a run."""
    api, transport = api_with_runs([workflow_run("Tests", "in_progress")])
    assert decide_runs(api, completion("Lint"), workflow_ref=workflow_ref)["review"] is True
    assert not read(transport, CALLER_ROUTE)
    assert not read(transport, RUNS_ROUTE)


def test_the_ref_is_used_when_the_runner_gives_no_workflow_sha():
    api, transport = api_with()
    route = f"/repos/{REPO}/contents/.github/workflows/code-review.yml?ref=refs/heads/master"
    transport.add("GET", route, data=b64(CALLER))
    transport.add(
        "GET",
        RUNS_ROUTE,
        data={"workflow_runs": [workflow_run("Lint"), workflow_run("Tests", "in_progress")]},
    )
    assert decide_runs(api, completion("Lint"), workflow_sha="")["review"] is False


def test_an_event_with_no_head_commit_reads_no_run():
    api, transport = api_with_runs([workflow_run("Tests", "in_progress")])
    event = completion("Lint")
    del event["workflow_run"]["head_sha"]
    event["workflow_run"]["pull_requests"] = [{"number": 42}]
    assert decide_runs(api, event)["review"] is True
    assert not read(transport, RUNS_ROUTE)


def test_every_page_of_runs_is_read():
    api, transport = api_with()
    transport.add("GET", CALLER_ROUTE, data=b64(CALLER))
    first_page = [workflow_run("Lint", run_id=1000 + n) for n in range(100)]
    transport.add("GET", RUNS_ROUTE, data={"workflow_runs": first_page})
    transport.add(
        "GET",
        RUNS_ROUTE.replace("&page=1", "&page=2"),
        data={"workflow_runs": [workflow_run("Tests", "in_progress")]},
    )
    assert decide_runs(api, completion("Lint"))["review"] is False


def test_the_command_passes_the_runners_workflow_ref_and_sha(tmp_path, monkeypatch):
    """The runner sets both in every job, so review.yml passes nothing and a
    review.yml and bot-ref mismatch breaks neither side."""
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(EVENT))
    seen = {}
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", WORKFLOW_REF)
    monkeypatch.setenv("GITHUB_WORKFLOW_SHA", WORKFLOW_SHA)

    def fake(**kwargs):
        seen.update(kwargs)
        return {"review": True, "reason": "because"}

    monkeypatch.setattr(cli, "preflight", fake)
    assert cli.main(["preflight", "--event", str(event_path), "--repo", REPO]) == 0
    assert seen["workflow_ref"] == WORKFLOW_REF
    assert seen["workflow_sha"] == WORKFLOW_SHA
