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


def issue_copy(transport):
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
                "--attempts",
                "1",
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
                "--attempts",
                "1",
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
            "--attempts",
            "1",
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
            "--attempts",
            "1",
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
