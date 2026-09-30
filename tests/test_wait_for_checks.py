"""Tests for `reviewbot wait-for-checks`.

A fake GitHub answers the policy and a scripted list of check runs per read,
and a fake clock that only moves when the code sleeps stands in for time, so
no test waits. The last tests use the real client over a fake transport, to pin
the requests themselves.

Run: pytest tests/test_wait_for_checks.py
"""

import base64

import pytest
import requests

from reviewbot import cli, github
from reviewbot.github import GitHubError
from tests.conftest import FakeTransport

LISTED = 'wait_for_checks: ["codecov/patch", "codecov/project"]\nwait_for_checks_seconds: 60\n'


def actions(status="completed", conclusion="success", name="Tests"):
    """A GitHub Actions check run in the given status."""
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion if status == "completed" else "",
    }


def codecov(name, status="completed"):
    """A Codecov check run with the given name and status."""
    return {
        "name": name,
        "status": status,
        "conclusion": "success" if status == "completed" else "",
    }


BOTH = [codecov("codecov/patch"), codecov("codecov/project")]


class FakeGitHub:
    """Answers the pull request, the policy, and one scripted read of the check
    runs per call, repeating the last one; records what each read asked for."""

    def __init__(self, policy, reads):
        """Keep the policy text and the reads, with no request recorded yet."""
        self.policy = policy
        self.reads = list(reads)
        self.policy_reads = []
        self.check_reads = []

    @property
    def seen(self):
        """How many times the check runs were read."""
        return len(self.check_reads)

    def pull_request(self, number):
        """A pull request from `abc` into `release/2.0`."""
        return {"base": {"ref": "release/2.0"}, "head": {"sha": "abc"}}

    def file_at_ref(self, path, ref):
        """The policy text, recording the path and ref asked for."""
        self.policy_reads.append((path, ref))
        return self.policy

    def check_runs(self, sha, exclude_check_name):
        """The next scripted read, raised when it is an exception."""
        self.check_reads.append((sha, exclude_check_name))
        read = self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
        if isinstance(read, Exception):
            raise read
        return read


class FakeClock:
    """A monotonic clock that moves forward only when the code sleeps."""

    START = 1000.0

    def __init__(self):
        """Start away from zero, with no sleep recorded."""
        self.now = self.START
        self.slept = []

    def __call__(self):
        """The current time."""
        return self.now

    @property
    def waited(self):
        """The seconds slept since the start."""
        return self.now - self.START

    def sleep(self, seconds):
        """Record the sleep and move the clock forward by it."""
        self.slept.append(seconds)
        self.now += seconds


def wait(api, clock, force=False):
    """Run the wait against the fakes and return its exit code."""
    return cli.wait_for_checks(
        repo="o/r",
        token="t",
        pr_number=1,
        force=force,
        api=api,
        sleep=clock.sleep,
        clock=clock,
    )


def test_it_waits_until_every_listed_check_exists_and_finishes(capsys):
    """The wait polls until both Codecov checks have appeared and finished,
    reading the base branch's policy and the head commit's check runs."""
    api = FakeGitHub(
        LISTED,
        [
            [actions()],
            [actions(), codecov("codecov/project", "in_progress")],
            [actions(), *BOTH],
        ],
    )
    clock = FakeClock()

    assert wait(api, clock) == 0

    assert api.seen == 3
    assert clock.slept == [cli.WAIT_POLL_SECONDS] * 2
    assert api.policy_reads == [(cli.POLICY_PATH, "release/2.0")]
    assert api.check_reads == [("abc", "Code review")] * 3
    assert "codecov/patch, codecov/project finished" in capsys.readouterr().out


def test_a_listed_check_still_running_keeps_the_wait_going(capsys):
    """Both checks exist, but one has not finished, so the wait goes on."""
    api = FakeGitHub(
        LISTED,
        [
            [actions(), codecov("codecov/patch"), codecov("codecov/project", "in_progress")],
            [actions(), *BOTH],
        ],
    )
    clock = FakeClock()

    assert wait(api, clock) == 0

    assert api.seen == 2
    assert clock.slept == [cli.WAIT_POLL_SECONDS]
    assert "finished" in capsys.readouterr().out


def test_an_unrelated_actions_workflow_still_running_keeps_the_wait_going(capsys):
    """A running Actions job whose workflow does not trigger the review cannot
    bring it back, so the wait goes on until it finishes too."""
    api = FakeGitHub(
        LISTED,
        [
            [actions(), actions("in_progress", name="Analyze (python)"), *BOTH],
            [actions(), actions(name="Analyze (python)"), *BOTH],
        ],
    )
    clock = FakeClock()

    assert wait(api, clock) == 0

    assert api.seen == 2
    assert clock.slept == [cli.WAIT_POLL_SECONDS]
    assert "codecov/patch, codecov/project finished" in capsys.readouterr().out


@pytest.mark.parametrize("conclusion", github.FAILED_CONCLUSIONS)
def test_red_ci_ends_the_wait_at_once(capsys, conclusion):
    """A check the review counts as red makes it skip whatever else reports, so
    there is nothing to wait for."""
    api = FakeGitHub(
        LISTED, [[actions(conclusion=conclusion, name="Lint"), actions("in_progress")]]
    )
    clock = FakeClock()

    assert wait(api, clock) == 0

    assert api.seen == 1
    assert clock.slept == []
    assert "CI is already red" in capsys.readouterr().out


def test_the_wait_names_the_head_it_polled(tmp_path, monkeypatch):
    """The polled commit becomes the step output `head`, which pins the review."""
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    api = FakeGitHub(LISTED, [[actions(), *BOTH]])

    assert wait(api, FakeClock()) == 0

    assert output.read_text() == "head=abc\n"


@pytest.mark.parametrize(
    ("policy", "force", "reads"),
    [
        ("", False, [[actions()]]),
        (LISTED, True, [[actions()]]),
        (LISTED + "require_ci_green: false\n", False, [[actions()]]),
        ("wait_for_checks: [1]\n", False, [[actions()]]),
        (LISTED, False, [GitHubError("GET check-runs: 502")]),
    ],
)
def test_no_head_is_named_when_nothing_was_waited_for(tmp_path, monkeypatch, policy, force, reads):
    """Without a wait there is nothing to pin, so the review runs as before."""
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    assert wait(FakeGitHub(policy, reads), FakeClock(), force=force) == 0

    assert not output.exists()


def test_it_gives_up_after_the_policy_seconds(capsys):
    """A listed check that never appears costs the whole wait and no more, then
    the review goes on without it."""
    api = FakeGitHub(LISTED, [[actions(), codecov("codecov/project")]])
    clock = FakeClock()

    assert wait(api, clock) == 0

    assert clock.waited == 60
    assert api.seen == 7
    assert "codecov/patch did not appear within 60s" in capsys.readouterr().out


def test_the_last_sleep_ends_at_the_deadline():
    """A cap that is not a multiple of the poll interval is not overrun."""
    policy = 'wait_for_checks: ["codecov/patch"]\nwait_for_checks_seconds: 15\n'
    api = FakeGitHub(policy, [[actions()]])
    clock = FakeClock()

    assert wait(api, clock) == 0

    assert clock.slept == [cli.WAIT_POLL_SECONDS, 5.0]
    assert api.seen == 3


def test_a_check_still_running_at_the_deadline_is_reported_as_unfinished(capsys):
    """The review skips while a check runs, and the log says so."""
    api = FakeGitHub(
        LISTED, [[actions(), codecov("codecov/patch"), codecov("codecov/project", "in_progress")]]
    )
    clock = FakeClock()

    assert wait(api, clock) == 0

    assert clock.waited == 60
    out = capsys.readouterr().out
    assert "codecov/project still running after 60s" in out
    assert "the review skips while CI is unfinished" in out


def test_nothing_is_read_when_no_check_is_listed():
    """The default policy lists no check, so no check run is read."""
    api = FakeGitHub("", [[actions()]])

    assert wait(api, FakeClock()) == 0

    assert api.seen == 0


def test_a_forced_review_does_not_wait():
    """A forced review does not gate on CI, so there is nothing to wait for."""
    api = FakeGitHub(LISTED, [[actions()]])

    assert wait(api, FakeClock(), force=True) == 0

    assert api.seen == 0
    assert api.policy_reads == []


def test_a_review_that_does_not_gate_on_ci_does_not_wait():
    """Without require_ci_green the wait is skipped."""
    api = FakeGitHub(LISTED + "require_ci_green: false\n", [[actions()]])

    assert wait(api, FakeClock()) == 0

    assert api.seen == 0


def test_a_malformed_policy_leaves_the_decision_to_the_review(capsys):
    """The review reports a malformed policy; the wait does not fail the job."""
    api = FakeGitHub("wait_for_checks: [1]\n", [[actions()]])

    assert wait(api, FakeClock()) == 0

    assert api.seen == 0
    assert "the review decides without waiting" in capsys.readouterr().out


def test_an_api_error_leaves_the_decision_to_the_review(capsys):
    """A failed read ends the wait instead of failing the job."""
    api = FakeGitHub(LISTED, [GitHubError("GET check-runs: 502")])

    assert wait(api, FakeClock()) == 0

    assert "the review decides without waiting" in capsys.readouterr().out


def raising_transport(method, url, headers, body):
    """A transport whose connection always drops."""
    raise requests.ConnectionError("connection reset by peer")


def test_a_dropped_connection_leaves_the_decision_to_the_review(capsys):
    """A transport error is not a GitHubError, and it must not fail the job
    either."""
    api = github.GitHub("o/r", "t", transport=raising_transport, sleep=lambda s: None)

    assert wait(api, FakeClock()) == 0

    out = capsys.readouterr().out
    assert "ConnectionError" in out
    assert "the review decides without waiting" in out


def policy_content(text):
    """A contents-API answer carrying `text`."""
    return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}


def test_the_real_client_reads_the_base_policy_and_the_head_check_runs(capsys):
    """Over the real client, the policy comes from the base branch and the
    check runs from the head commit."""
    transport = FakeTransport()
    transport.add(
        "GET",
        "/repos/o/r/pulls/1",
        data={"base": {"ref": "release/2.0"}, "head": {"sha": "abc"}},
    )
    transport.add(
        "GET",
        "/repos/o/r/contents/.github/code-review/policy.yml?ref=release/2.0",
        data=policy_content(LISTED),
    )
    transport.add(
        "GET",
        "/repos/o/r/commits/abc/check-runs?per_page=100",
        data={"check_runs": [actions(), *BOTH]},
    )
    api = github.GitHub("o/r", "t", transport=transport, sleep=lambda s: None)

    assert wait(api, FakeClock()) == 0

    assert "codecov/patch, codecov/project finished" in capsys.readouterr().out
    assert [call["path"] for call in transport.calls] == [
        "/repos/o/r/pulls/1",
        "/repos/o/r/contents/.github/code-review/policy.yml?ref=release/2.0",
        "/repos/o/r/commits/abc/check-runs?per_page=100",
    ]


def test_the_command_reaches_the_wait(monkeypatch):
    """`reviewbot wait-for-checks` passes the pull request, the repository and
    the token to the wait."""
    seen = {}
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.delenv("REVIEWBOT_FORCE", raising=False)
    monkeypatch.setattr(cli, "wait_for_checks", lambda **kwargs: seen.update(kwargs) or 0)

    assert cli.main(["wait-for-checks", "--pr", "7", "--repo", "o/r"]) == 0

    assert seen == {"repo": "o/r", "token": "t", "pr_number": 7, "force": False}


def test_a_forced_run_reaches_the_wait_as_forced(monkeypatch):
    """REVIEWBOT_FORCE, which the workflow sets from its `force` input, reaches
    the wait."""
    seen = {}
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("REVIEWBOT_FORCE", "true")
    monkeypatch.setattr(cli, "wait_for_checks", lambda **kwargs: seen.update(kwargs) or 0)

    assert cli.main(["wait-for-checks", "--pr", "7", "--repo", "o/r"]) == 0

    assert seen["force"] is True
