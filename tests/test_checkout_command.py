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
