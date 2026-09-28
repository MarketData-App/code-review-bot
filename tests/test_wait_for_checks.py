"""Tests for `reviewbot wait-for-checks`.

A fake GitHub answers the policy and a scripted list of check runs per read,
and a fake clock that only moves when the code sleeps stands in for time, so
no test waits.

Run: pytest tests/test_wait_for_checks.py
"""

from reviewbot import cli
from reviewbot.github import GitHubError

LISTED = 'wait_for_checks: ["codecov/patch", "codecov/project"]\nwait_for_checks_seconds: 60\n'


def actions(status):
    """A GitHub Actions check run in the given status."""
    return {"name": "Tests", "status": status, "app": "github-actions"}


def codecov(name, status="completed"):
    """A Codecov check run with the given name and status."""
    return {"name": name, "status": status, "app": "codecov"}


class FakeGitHub:
    """Answers the pull request, the policy, and one scripted read of the check
    runs per call, repeating the last one."""

    def __init__(self, policy, reads):
        """Keep the policy text and the reads, and count the reads made."""
        self.policy = policy
        self.reads = list(reads)
        self.seen = 0

    def pull_request(self, number):
        """A pull request on `main` whose head is `abc`."""
        return {"base": {"ref": "main"}, "head": {"sha": "abc"}}

    def file_at_ref(self, path, ref):
        """The policy text, whatever the path."""
        return self.policy

    def check_runs(self, sha, exclude_check_name):
        """The next scripted read, raised when it is an exception."""
        self.seen += 1
        read = self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
        if isinstance(read, Exception):
            raise read
        return read


class FakeClock:
    """A monotonic clock that moves forward only when the code sleeps."""

    def __init__(self):
        """Start at zero, with no sleep recorded."""
        self.now = 0.0
        self.slept = []

    def __call__(self):
        """The current time."""
        return self.now

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
    """The wait polls until both Codecov checks have appeared and finished."""
    api = FakeGitHub(
        LISTED,
        [
            [actions("completed")],
            [actions("completed"), codecov("codecov/project", "in_progress")],
            [actions("completed"), codecov("codecov/project"), codecov("codecov/patch")],
        ],
    )
    clock = FakeClock()

    assert wait(api, clock) == 0

    assert api.seen == 3
    assert clock.slept == [cli.WAIT_POLL_SECONDS] * 2
    assert "codecov/patch, codecov/project finished" in capsys.readouterr().out


def test_a_running_actions_job_ends_the_wait_at_once(capsys):
    """Its workflow triggers the review again when it finishes, so the earlier
    trigger still skips at once instead of waiting beside the later one."""
    api = FakeGitHub(LISTED, [[actions("in_progress"), codecov("codecov/project", "queued")]])
    clock = FakeClock()

    assert wait(api, clock) == 0

    assert clock.slept == []
    assert "a GitHub Actions job is still running" in capsys.readouterr().out


def test_it_gives_up_after_the_policy_seconds(capsys):
    """A listed check that never appears costs the whole wait, then the review
    goes on without it."""
    api = FakeGitHub(LISTED, [[actions("completed"), codecov("codecov/project")]])
    clock = FakeClock()

    assert wait(api, clock) == 0

    assert clock.now >= 60
    assert "codecov/patch did not finish within 60s" in capsys.readouterr().out


def test_nothing_is_read_when_no_check_is_listed():
    """The default policy lists no check, so the step reads nothing."""
    api = FakeGitHub("", [[actions("completed")]])

    assert wait(api, FakeClock()) == 0

    assert api.seen == 0


def test_a_forced_review_does_not_wait():
    """A forced review does not gate on CI, so there is nothing to wait for."""
    api = FakeGitHub(LISTED, [[actions("completed")]])

    assert wait(api, FakeClock(), force=True) == 0

    assert api.seen == 0


def test_a_review_that_does_not_gate_on_ci_does_not_wait():
    """Without require_ci_green the listed checks change nothing."""
    api = FakeGitHub(LISTED + "require_ci_green: false\n", [[actions("completed")]])

    assert wait(api, FakeClock()) == 0

    assert api.seen == 0


def test_a_malformed_policy_leaves_the_decision_to_the_review(capsys):
    """The review reports a malformed policy; the wait does not fail the job."""
    api = FakeGitHub("wait_for_checks: [1]\n", [[actions("completed")]])

    assert wait(api, FakeClock()) == 0

    assert api.seen == 0
    assert "the review decides without waiting" in capsys.readouterr().out


def test_an_api_error_leaves_the_decision_to_the_review(capsys):
    """A failed read ends the wait instead of failing the job."""
    api = FakeGitHub(LISTED, [GitHubError("GET check-runs: 502")])

    assert wait(api, FakeClock()) == 0

    assert "the review decides without waiting" in capsys.readouterr().out


def test_the_command_reaches_the_wait(monkeypatch):
    """`reviewbot wait-for-checks` passes the pull request, the repository and
    the token to the wait."""
    seen = {}
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.delenv("REVIEWBOT_FORCE", raising=False)
    monkeypatch.setattr(cli, "wait_for_checks", lambda **kwargs: seen.update(kwargs) or 0)

    assert cli.main(["wait-for-checks", "--pr", "7", "--repo", "o/r"]) == 0

    assert seen == {"repo": "o/r", "token": "t", "pr_number": 7, "force": False}
