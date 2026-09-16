"""The Claude token comes from the store, not from six repository secrets.

Rotating it meant editing six places -- an org secret plus five `sdk-*` repo
secrets -- and any one missed fails silently later. An org secret cannot reach
those repos anyway: they live on the MarketDataApp USER account, and an org
secret only reaches repos in the org. And a secret's value can never be read
back (`GET /orgs/.../secrets/...` returns no `value` field), so "read it from
the org" has to mean reading a FILE from the private store with the org App
installation token -- the same path the Codex credential already takes.

Unlike Codex this needs no lease: `CLAUDE_CODE_OAUTH_TOKEN` is a static bearer
token and concurrent use is normal, which is how the shared org secret already
works.

Run: pytest tests/test_claude_token.py
"""

import base64

import pytest

from reviewbot import cli, credentials, github
from tests.conftest import FakeTransport

STORE = "MarketData-App/code-review-credentials"
TOKEN_VALUE = "sk-ant-oat01-" + "T" * 20


@pytest.fixture
def transport(monkeypatch, tmp_path):
    fake = FakeTransport()
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_" + "T" * 36)
    monkeypatch.setenv("GITHUB_ENV", str(tmp_path / "env"))
    monkeypatch.setattr(
        cli,
        "_store_api",
        lambda repo, token: github.GitHub(repo, token, transport=fake, sleep=lambda s: None),
    )
    return fake


def stored(transport, value=TOKEN_VALUE):
    transport.add(
        "GET",
        f"/repos/{STORE}/contents/{credentials.CLAUDE_PATH}?ref=issue",
        data={
            "encoding": "base64",
            "content": base64.b64encode(value.encode()).decode(),
            "sha": "c1",
        },
    )


def env_file(monkeypatch_tmp):
    return (monkeypatch_tmp / "env").read_text() if (monkeypatch_tmp / "env").exists() else ""


def test_the_token_is_fetched_and_exported(transport, tmp_path, capsys):
    stored(transport)
    assert cli.main(["credential-claude", "--store", STORE]) == 0
    written = (tmp_path / "env").read_text()
    assert f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN_VALUE}" in written
    assert "fetched=true" in capsys.readouterr().out


def test_the_token_is_masked_before_it_is_exported(transport, tmp_path, capsys):
    stored(transport)
    cli.main(["credential-claude", "--store", STORE])
    out = capsys.readouterr().out
    assert f"::add-mask::{TOKEN_VALUE}" in out
    assert out.index("::add-mask::") < out.index("fetched=true")


def test_surrounding_whitespace_is_stripped(transport, tmp_path):
    stored(transport, value=f"\n  {TOKEN_VALUE}  \n")
    cli.main(["credential-claude", "--store", STORE])
    assert f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN_VALUE}\n" in (tmp_path / "env").read_text()


def test_a_missing_token_is_not_fatal(transport, tmp_path, capsys):
    # A repository that still carries its own secret must keep working.
    transport.add(
        "GET",
        f"/repos/{STORE}/contents/{credentials.CLAUDE_PATH}?ref=issue",
        status=404,
        text="Not Found",
    )
    assert cli.main(["credential-claude", "--store", STORE]) == 0
    assert "fetched=false" in capsys.readouterr().out
    assert (
        not (tmp_path / "env").exists()
        or "CLAUDE_CODE_OAUTH_TOKEN" not in (tmp_path / "env").read_text()
    )


def test_an_unreachable_store_is_not_fatal(transport, tmp_path, capsys):
    transport.add(
        "GET",
        f"/repos/{STORE}/contents/{credentials.CLAUDE_PATH}?ref=issue",
        status=500,
        text="boom",
    )
    assert cli.main(["credential-claude", "--store", STORE]) == 0
    assert "fetched=false" in capsys.readouterr().out


def test_it_never_overwrites_a_token_the_repository_already_set(
    transport, tmp_path, capsys, monkeypatch
):
    # An explicit repository secret wins: it is the escape hatch for a repo
    # that must not use the shared plan.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "already-set")
    stored(transport)
    assert cli.main(["credential-claude", "--store", STORE]) == 0
    out = capsys.readouterr().out
    assert "already set" in out
    assert "fetched=false" in out
