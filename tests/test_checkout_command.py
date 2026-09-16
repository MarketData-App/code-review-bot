"""Tests for the check-out and check-in commands.

Neither may fail a review. A credential the job cannot borrow means the Codex
backend is skipped and the review runs with Claude, which is what
`backends.run()` already does with a backend that cannot start.

Run: pytest tests/test_checkout_command.py
"""

import base64
import json
import stat

import pytest

from reviewbot import cli, credentials, github
from tests.conftest import FakeTransport

STORE = "MarketData-App/code-review-credentials"
ISSUE = {
    "auth_mode": "chatgpt",
    "tokens": {"access_token": "AAA", "refresh_token": credentials.PLACEHOLDER},
}


@pytest.fixture
def transport(monkeypatch):
    fake = FakeTransport()
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_" + "T" * 36)
    monkeypatch.setattr(
        cli,
        "_store_api",
        lambda repo, token: github.GitHub(repo, token, transport=fake, sleep=lambda s: None),
    )
    return fake


def free_lease(transport):
    transport.add(
        "GET",
        f"/repos/{STORE}/contents/codex/lease.json?ref=main",
        data={
            "encoding": "base64",
            "content": base64.b64encode(b'{"holder": null}').decode(),
            "sha": "b1",
        },
    )
    transport.add("PUT", f"/repos/{STORE}/contents/codex/lease.json", data={"commit": {}})


def issue_copy(transport, expires_at="2099-01-01T00:00:00+00:00"):
    transport.add(
        "GET",
        f"/repos/{STORE}/contents/codex/meta.json?ref=issue",
        data={
            "encoding": "base64",
            "content": base64.b64encode(json.dumps({"expires_at": expires_at}).encode()).decode(),
            "sha": "m1",
        },
    )
    transport.add(
        "GET",
        f"/repos/{STORE}/contents/codex/auth.json?ref=issue",
        data={
            "encoding": "base64",
            "content": base64.b64encode(json.dumps(ISSUE).encode()).decode(),
            "sha": "b2",
        },
    )


def test_checkout_writes_the_credential_and_reports_fetched(tmp_path, transport, capsys):
    free_lease(transport)
    issue_copy(transport)
    home = tmp_path / "codex-home"
    code = cli.main(
        [
            "credential-checkout",
            "--store",
            STORE,
            "--holder",
            "sdk-py#100",
            "--run-url",
            "https://run/1",
            "--codex-home",
            str(home),
        ]
    )
    assert code == 0
    assert (
        json.loads((home / "auth.json").read_text())["tokens"]["refresh_token"]
        == credentials.PLACEHOLDER
    )
    assert stat.S_IMODE((home / "auth.json").stat().st_mode) == 0o600
    assert "fetched=true" in capsys.readouterr().out


def test_checkout_masks_every_token_value_in_the_log(tmp_path, transport, capsys):
    free_lease(transport)
    issue_copy(transport)
    cli.main(
        [
            "credential-checkout",
            "--store",
            STORE,
            "--holder",
            "sdk-py#100",
            "--run-url",
            "https://run/1",
            "--codex-home",
            str(tmp_path / "h"),
        ]
    )
    assert "::add-mask::AAA" in capsys.readouterr().out


def test_a_held_lease_reports_not_fetched_and_still_exits_zero(tmp_path, transport, capsys):
    transport.add(
        "GET",
        f"/repos/{STORE}/contents/codex/lease.json?ref=main",
        data={
            "encoding": "base64",
            "content": base64.b64encode(
                json.dumps(
                    {"holder": "sdk-go#41", "expires_at": "2099-01-01T00:00:00+00:00"}
                ).encode()
            ).decode(),
            "sha": "b1",
        },
    )
    home = tmp_path / "codex-home"
    assert (
        cli.main(
            [
                "credential-checkout",
                "--store",
                STORE,
                "--holder",
                "sdk-py#100",
                "--run-url",
                "https://run/1",
                "--codex-home",
                str(home),
                "--wait-minutes",
                "0",
            ]
        )
        == 0
    )
    assert not (home / "auth.json").exists()
    assert "fetched=false" in capsys.readouterr().out


def test_an_unreachable_store_reports_not_fetched_and_still_exits_zero(tmp_path, transport, capsys):
    transport.add(
        "GET", f"/repos/{STORE}/contents/codex/lease.json?ref=main", status=500, text="boom"
    )
    assert (
        cli.main(
            [
                "credential-checkout",
                "--store",
                STORE,
                "--holder",
                "sdk-py#100",
                "--run-url",
                "https://run/1",
                "--codex-home",
                str(tmp_path / "h"),
                "--wait-minutes",
                "0",
            ]
        )
        == 0
    )
    assert "fetched=false" in capsys.readouterr().out


def test_checkin_deletes_the_credential_before_it_frees_the_lease(tmp_path, transport):
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    transport.add(
        "GET",
        f"/repos/{STORE}/contents/codex/lease.json?ref=main",
        data={
            "encoding": "base64",
            "content": base64.b64encode(json.dumps({"holder": "sdk-py#100"}).encode()).decode(),
            "sha": "b1",
        },
    )
    transport.add("PUT", f"/repos/{STORE}/contents/codex/lease.json", data={"commit": {}})
    assert (
        cli.main(
            [
                "credential-checkin",
                "--store",
                STORE,
                "--holder",
                "sdk-py#100",
                "--codex-home",
                str(home),
            ]
        )
        == 0
    )
    assert not home.exists()


def test_checkin_deletes_the_credential_even_when_the_store_is_unreachable(tmp_path, transport):
    # The file on the runner matters more than the lease record: the lease
    # expires by itself, a credential left on a persistent runner does not.
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    transport.add(
        "GET", f"/repos/{STORE}/contents/codex/lease.json?ref=main", status=500, text="boom"
    )
    assert (
        cli.main(
            [
                "credential-checkin",
                "--store",
                STORE,
                "--holder",
                "sdk-py#100",
                "--codex-home",
                str(home),
            ]
        )
        == 0
    )
    assert not home.exists()


# --- fix round 1: the "never fail the review" guarantee must not break -----


def held_lease_by_our_holder(transport, sha="b3"):
    """A second lease read, used by the release call that follows an acquire."""
    transport.add(
        "GET",
        f"/repos/{STORE}/contents/codex/lease.json?ref=main",
        data={
            "encoding": "base64",
            "content": base64.b64encode(json.dumps({"holder": "sdk-py#100"}).encode()).decode(),
            "sha": sha,
        },
    )
    transport.add("PUT", f"/repos/{STORE}/contents/codex/lease.json", data={"commit": {}})


def test_checkout_survives_an_issued_credential_that_is_not_a_json_object(
    tmp_path, transport, capsys
):
    # json.loads("5") succeeds -- it is valid JSON -- but the result has no
    # .get. The masking loop must not crash the command on a store reply that
    # is valid JSON but not an object.
    free_lease(transport)
    transport.add(
        "GET",
        f"/repos/{STORE}/contents/codex/auth.json?ref=issue",
        data={"encoding": "base64", "content": base64.b64encode(b"5").decode(), "sha": "b2"},
    )
    code = cli.main(
        [
            "credential-checkout",
            "--store",
            STORE,
            "--holder",
            "sdk-py#100",
            "--run-url",
            "https://run/1",
            "--codex-home",
            str(tmp_path / "codex-home"),
        ]
    )
    assert code == 0


def test_checkout_releases_the_lease_when_the_write_fails(tmp_path, transport, capsys):
    # codex-home already exists as a plain file, so Path.mkdir(exist_ok=True)
    # raises FileExistsError -- a write failure discovered only after the
    # lease is already held.
    free_lease(transport)
    issue_copy(transport)
    held_lease_by_our_holder(transport)
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    code = cli.main(
        [
            "credential-checkout",
            "--store",
            STORE,
            "--holder",
            "sdk-py#100",
            "--run-url",
            "https://run/1",
            "--codex-home",
            str(blocked),
        ]
    )
    assert code == 0
    assert "fetched=false" in capsys.readouterr().out
    releases = [
        call
        for call in transport.calls
        if call["method"] == "PUT" and call["path"] == f"/repos/{STORE}/contents/codex/lease.json"
    ]
    assert len(releases) == 2  # one to acquire the lease, one to free it


def _raising_transport(method, url, headers, body):
    raise OSError("network down")


def test_checkout_survives_a_raw_transport_error(tmp_path, monkeypatch, capsys):
    # requests can raise ConnectionError/Timeout/SSLError directly; those are
    # not GitHubError, and must not escape credential_checkout either.
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_" + "T" * 36)
    monkeypatch.setattr(
        cli,
        "_store_api",
        lambda repo, token: github.GitHub(
            repo, token, transport=_raising_transport, sleep=lambda s: None
        ),
    )
    code = cli.main(
        [
            "credential-checkout",
            "--store",
            STORE,
            "--holder",
            "sdk-py#100",
            "--run-url",
            "https://run/1",
            "--codex-home",
            str(tmp_path / "h"),
            "--wait-minutes",
            "0",
        ]
    )
    assert code == 0
    assert "fetched=false" in capsys.readouterr().out


def test_checkin_survives_a_raw_transport_error(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_" + "T" * 36)
    monkeypatch.setattr(
        cli,
        "_store_api",
        lambda repo, token: github.GitHub(
            repo, token, transport=_raising_transport, sleep=lambda s: None
        ),
    )
    code = cli.main(
        [
            "credential-checkin",
            "--store",
            STORE,
            "--holder",
            "sdk-py#100",
            "--codex-home",
            str(home),
        ]
    )
    assert code == 0
    assert not home.exists()


# --- fix round 2: the value the workflow step reads back ------------------


def test_the_output_key_is_written_exactly_once(tmp_path, transport, monkeypatch):
    # The borrow step appends `fetched=false` itself when the command fails
    # before it can speak. That fallback is only safe while the command writes
    # at most one `fetched=` line and exits 0 whenever it has written one:
    # otherwise a later `steps.codex.outputs.fetched == 'true'` could read a
    # doubled or stale value.
    free_lease(transport)
    issue_copy(transport)
    output = tmp_path / "gh-output"
    output.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    code = cli.main(
        [
            "credential-checkout",
            "--store",
            STORE,
            "--holder",
            "sdk-py#100#551-1",
            "--run-url",
            "https://run/1",
            "--codex-home",
            str(tmp_path / "codex-home"),
        ]
    )
    assert code == 0
    lines = [ln for ln in output.read_text().splitlines() if ln.startswith("fetched=")]
    assert lines == ["fetched=true"]


def test_a_failure_writes_one_definite_output_and_exits_zero(tmp_path, transport, monkeypatch):
    transport.add(
        "GET", f"/repos/{STORE}/contents/codex/lease.json?ref=main", status=500, text="boom"
    )
    output = tmp_path / "gh-output"
    output.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    code = cli.main(
        [
            "credential-checkout",
            "--store",
            STORE,
            "--holder",
            "sdk-py#100#551-1",
            "--run-url",
            "https://run/1",
            "--codex-home",
            str(tmp_path / "h"),
            "--wait-minutes",
            "0",
        ]
    )
    assert code == 0
    lines = [ln for ln in output.read_text().splitlines() if ln.startswith("fetched=")]
    assert lines == ["fetched=false"]


def test_a_missing_token_exits_before_the_command_can_report(tmp_path, monkeypatch):
    """Why the workflow step carries `|| echo fetched=false`.

    The organisation token is minted `continue-on-error`, so an empty
    GITHUB_TOKEN is an expected state rather than a bug. argparse answers it
    with SystemExit(2) BEFORE `credential_checkout` is entered, so the total
    `except Exception` inside it never sees this one and the step -- and the
    whole review -- goes red. The fallback in the workflow is what catches it;
    tests/test_workflow.py asserts the fallback is there.
    """
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("REVIEWBOT_TOKEN", raising=False)
    with pytest.raises(SystemExit) as caught:
        cli.main(
            [
                "credential-checkout",
                "--store",
                STORE,
                "--holder",
                "sdk-py#100#551-1",
                "--run-url",
                "https://run/1",
                "--codex-home",
                str(tmp_path / "h"),
            ]
        )
    assert caught.value.code == 2


def test_the_borrowed_credential_directory_is_not_world_readable(tmp_path, transport):
    free_lease(transport)
    issue_copy(transport)
    home = tmp_path / "codex-home"
    cli.main(
        [
            "credential-checkout",
            "--store",
            STORE,
            "--holder",
            "sdk-py#100#551-1",
            "--run-url",
            "https://run/1",
            "--codex-home",
            str(home),
        ]
    )
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


# --- do not take the lease for a repository that will not run codex ---------


def policy_route(transport, body, base="main"):
    transport.add(
        "GET",
        "/repos/MarketDataApp/sdk-py/pulls/7",
        data={"base": {"ref": base}},
    )
    transport.add(
        "GET",
        f"/repos/MarketDataApp/sdk-py/contents/.github/code-review/policy.yml?ref={base}",
        data={
            "encoding": "base64",
            "content": base64.b64encode(body.encode()).decode(),
            "sha": "p1",
        },
    )


def test_a_repository_whose_policy_omits_codex_never_takes_the_lease(tmp_path, transport, capsys):
    # The default policy is `backends: [claude, codex]` with `mode: first`, so
    # Claude runs and Codex never does. Borrowing anyway would hold the shared
    # lease for the whole review and serialise every other repository's reviews
    # behind a backend that was never going to run.
    policy_route(transport, "backends: [claude]\n")
    home = tmp_path / "codex-home"
    code = cli.main(
        [
            "credential-checkout",
            "--store",
            STORE,
            "--holder",
            "MarketDataApp/sdk-py#7",
            "--run-url",
            "https://run/1",
            "--codex-home",
            str(home),
            "--repo",
            "MarketDataApp/sdk-py",
            "--pr",
            "7",
        ]
    )
    assert code == 0
    assert not home.exists()
    assert "fetched=false" in capsys.readouterr().out
    # The lease was never read or written.
    assert not any("lease.json" in c["path"] for c in transport.calls)


def test_the_default_policy_does_not_borrow_because_mode_first_never_reaches_codex(
    tmp_path, transport, capsys
):
    # The shipped default. `mode: first` runs ready[0], so Claude reviews and
    # Codex never does -- borrowing would hold the org-wide lease for nothing.
    policy_route(transport, "backends: [claude, codex]\nmode: first\n")
    home = tmp_path / "codex-home"
    assert (
        cli.main(
            [
                "credential-checkout",
                "--store",
                STORE,
                "--holder",
                "MarketDataApp/sdk-py#7",
                "--run-url",
                "https://run/1",
                "--codex-home",
                str(home),
                "--repo",
                "MarketDataApp/sdk-py",
                "--pr",
                "7",
            ]
        )
        == 0
    )
    assert "fetched=false" in capsys.readouterr().out
    assert not any("lease.json" in c["path"] for c in transport.calls)


def test_codex_first_under_mode_first_does_borrow(tmp_path, transport, capsys):
    policy_route(transport, "backends: [codex, claude]\nmode: first\n")
    free_lease(transport)
    issue_copy(transport)
    home = tmp_path / "codex-home"
    assert (
        cli.main(
            [
                "credential-checkout",
                "--store",
                STORE,
                "--holder",
                "MarketDataApp/sdk-py#7",
                "--run-url",
                "https://run/1",
                "--codex-home",
                str(home),
                "--repo",
                "MarketDataApp/sdk-py",
                "--pr",
                "7",
            ]
        )
        == 0
    )
    assert "fetched=true" in capsys.readouterr().out
    assert home.exists()


def test_an_unreadable_policy_borrows_rather_than_skipping(tmp_path, transport, capsys):
    # Fail toward borrowing: an unnecessary borrow wastes a lease slot, a
    # missed one silently drops the backend the repository asked for.
    transport.add("GET", "/repos/MarketDataApp/sdk-py/pulls/7", status=500, text="boom")
    free_lease(transport)
    issue_copy(transport)
    home = tmp_path / "codex-home"
    assert (
        cli.main(
            [
                "credential-checkout",
                "--store",
                STORE,
                "--holder",
                "MarketDataApp/sdk-py#7",
                "--run-url",
                "https://run/1",
                "--codex-home",
                str(home),
                "--repo",
                "MarketDataApp/sdk-py",
                "--pr",
                "7",
            ]
        )
        == 0
    )
    assert "fetched=true" in capsys.readouterr().out


# --- waiting for the shared lease ------------------------------------------


def test_a_held_lease_is_waited_for_not_abandoned(tmp_path, transport, capsys, monkeypatch):
    """One plan is shared, so a review that arrives second must queue.

    Giving up used to mean the backend was silently dropped -- and on a
    codex-only repository that is no review at all, not a degraded one.
    """
    naps = []
    monkeypatch.setattr(cli._time, "sleep", naps.append)
    held = json.dumps({"holder": "other#1", "expires_at": "2099-01-01T00:00:00+00:00"})
    for _ in range(3):
        transport.add(
            "GET",
            f"/repos/{STORE}/contents/codex/lease.json?ref=main",
            data={
                "encoding": "base64",
                "content": base64.b64encode(held.encode()).decode(),
                "sha": "b1",
            },
        )
    # then it frees, and we take it
    free_lease(transport)
    issue_copy(transport)
    home = tmp_path / "codex-home"
    assert (
        cli.main(
            [
                "credential-checkout",
                "--store",
                STORE,
                "--holder",
                "me#1",
                "--run-url",
                "u",
                "--codex-home",
                str(home),
                "--wait-minutes",
                "25",
                "--poll-seconds",
                "20",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "fetched=true" in out
    assert "waiting" in out
    assert naps, "it must actually sleep between attempts rather than spin"


def test_the_wait_is_bounded_and_gives_up_cleanly(tmp_path, transport, capsys, monkeypatch):
    monkeypatch.setattr(cli._time, "sleep", lambda s: None)
    held = json.dumps({"holder": "other#1", "expires_at": "2099-01-01T00:00:00+00:00"})
    transport.add(
        "GET",
        f"/repos/{STORE}/contents/codex/lease.json?ref=main",
        data={
            "encoding": "base64",
            "content": base64.b64encode(held.encode()).decode(),
            "sha": "b1",
        },
    )
    home = tmp_path / "codex-home"
    assert (
        cli.main(
            [
                "credential-checkout",
                "--store",
                STORE,
                "--holder",
                "me#1",
                "--run-url",
                "u",
                "--codex-home",
                str(home),
                "--wait-minutes",
                "0",
            ]
        )
        == 0
    )
    assert "fetched=false" in capsys.readouterr().out
    assert not home.exists()


# --- the lease is sized against the work it protects ------------------------


def test_the_ttl_outlasts_the_worst_case_review_and_the_wait_outlasts_the_ttl():
    """Three numbers, and their ORDER is the invariant.

    `Backend.review` is `for attempt in (1, 2)` around a call bounded by
    `timeout_minutes`, so one backend can legitimately run for twice that. A
    TTL shorter than that expires under a job still working and hands the
    credential to a second one. And a wait shorter than the TTL gives up just
    before a dead holder's lease would have freed itself.
    """
    for cap in (5, 15):
        ttl, wait = cli._lease_minutes({"timeout_minutes": cap})
        worst_case_review = 2 * cap
        assert ttl > worst_case_review, f"ttl {ttl} must outlast a {worst_case_review}m review"
        assert wait > ttl, f"wait {wait} must outlast the ttl {ttl}"


def test_a_large_timeout_sacrifices_the_wait_rather_than_the_job():
    """Above cap ~20 the two goals cannot both hold in a 90 minute job.

    ttl > 2*cap and wait > ttl and wait + 2*cap + overhead <= budget together
    require cap < 20. Past that, fitting inside the job wins: a wait the runner
    kills mid-queue looks like a failed review, whereas a wait shorter than the
    TTL only risks giving up while a DEAD holder's lease is still ticking.
    """
    ttl, wait = cli._lease_minutes({"timeout_minutes": 30})
    assert wait < ttl
    assert wait + 2 * 30 <= cli.JOB_BUDGET_MINUTES


def test_an_unreadable_policy_still_sizes_the_lease_from_the_shipped_default():
    ttl, wait = cli._lease_minutes(None)
    assert (ttl, wait) == (35.0, 40.0)


def test_fallback_mode_with_codex_second_does_not_borrow():
    # Under `fallback` Codex runs only when the backend before it fails, which
    # is rare. Holding the org-wide lease for every such review would starve
    # the repositories that reach Codex on every run.
    assert cli._will_run_codex({"backends": ["claude", "codex"], "mode": "fallback"}) is False
    assert cli._will_run_codex({"backends": ["codex", "claude"], "mode": "fallback"}) is True


def test_the_wait_never_outruns_the_job_budget():
    # A large timeout_minutes would otherwise size a wait the runner kills
    # mid-queue. wait + worst-case review must fit inside the job's budget.
    for cap in (5, 15, 30, 40):
        ttl, wait = cli._lease_minutes({"timeout_minutes": cap})
        assert wait + 2 * cap <= cli.JOB_BUDGET_MINUTES, f"cap={cap} ttl={ttl} wait={wait}"


def test_an_expired_issued_credential_is_not_borrowed(tmp_path, transport, capsys):
    """An expired copy authenticates nothing.

    Borrowing it spends the org-wide lease and a whole review slot to arrive at
    a certain failure, so `meta.json` is read before the credential is written.
    """
    free_lease(transport)
    issue_copy(transport, expires_at="2020-01-01T00:00:00+00:00")
    home = tmp_path / "codex-home"
    assert (
        cli.main(
            [
                "credential-checkout",
                "--store",
                STORE,
                "--holder",
                "me#1",
                "--run-url",
                "u",
                "--codex-home",
                str(home),
                "--wait-minutes",
                "0",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "expired" in out
    assert "fetched=false" in out
    assert not home.exists()
