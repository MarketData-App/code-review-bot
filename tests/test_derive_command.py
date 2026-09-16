"""Tests for `reviewbot derive-credential`.

The keeper on skynet runs this. It reads the vault, and writes the copy that
every review job will borrow. It reaches no network: the git force-push that
publishes the result lives in the self-hosted-runner repository.

Run: pytest tests/test_derive_command.py
"""

import base64
import datetime
import json
import stat

from reviewbot import cli, credentials


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwt_with_exp(exp: int) -> str:
    return f"{b64(b'{}')}.{b64(json.dumps({'exp': exp}).encode())}.{b64(b'sig')}"


def vault(tmp_path, hours_left: float):
    exp = int(
        (datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=hours_left)).timestamp()
    )
    home = tmp_path / "vault"
    home.mkdir()
    (home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": jwt_with_exp(exp),
                    "access_token": jwt_with_exp(exp),
                    "refresh_token": "a-real-looking-refresh-token",
                    "account_id": "acct-1",
                },
                "last_refresh": "2026-09-08T00:17:16.736282269Z",
            }
        )
    )
    return home


def test_it_writes_a_derived_copy_and_its_meta(tmp_path):
    home, out = vault(tmp_path, hours_left=240), tmp_path / "out"
    code = cli.main(["derive-credential", "--codex-home", str(home), "--out", str(out)])
    assert code == 0
    written = json.loads((out / "auth.json").read_text())
    assert written["tokens"]["refresh_token"] == credentials.PLACEHOLDER
    assert written["tokens"]["account_id"] == "acct-1"
    assert json.loads((out / "meta.json").read_text())["expires_at"].endswith("+00:00")


def test_the_vault_is_not_modified(tmp_path):
    home, out = vault(tmp_path, hours_left=240), tmp_path / "out"
    before = (home / "auth.json").read_text()
    cli.main(["derive-credential", "--codex-home", str(home), "--out", str(out)])
    assert (home / "auth.json").read_text() == before


def test_the_copy_is_not_world_readable(tmp_path):
    home, out = vault(tmp_path, hours_left=240), tmp_path / "out"
    cli.main(["derive-credential", "--codex-home", str(home), "--out", str(out)])
    assert stat.S_IMODE((out / "auth.json").stat().st_mode) == 0o600


def test_the_directory_holding_the_copy_is_not_world_readable(tmp_path):
    # A 0600 auth.json inside a 0755 directory still announces itself to every
    # other account on the machine. `credential_checkout` already holds
    # $CODEX_HOME at 0700; the keeper's output directory kept the umask
    # default, which on the runner image is world-readable.
    home, out = vault(tmp_path, hours_left=240), tmp_path / "out"
    cli.main(["derive-credential", "--codex-home", str(home), "--out", str(out)])
    assert stat.S_IMODE(out.stat().st_mode) == 0o700


def test_it_refuses_to_publish_a_token_that_expires_too_soon(tmp_path, capsys):
    # Publishing a credential that dies mid-review is worse than publishing
    # none: a missing copy degrades to a Claude-only review, an expiring one
    # fails in the middle of a job.
    home, out = vault(tmp_path, hours_left=12), tmp_path / "out"
    assert cli.main(["derive-credential", "--codex-home", str(home), "--out", str(out)]) == 1
    assert not (out / "auth.json").exists()
    assert "expires" in capsys.readouterr().out


def test_it_refuses_when_there_is_no_vault(tmp_path, capsys):
    assert (
        cli.main(
            [
                "derive-credential",
                "--codex-home",
                str(tmp_path / "nothing"),
                "--out",
                str(tmp_path / "out"),
            ]
        )
        == 1
    )
    assert "auth.json" in capsys.readouterr().out


def bad_vault(tmp_path, text: str):
    """A vault whose auth.json holds `text` verbatim, valid JSON or not."""
    home = tmp_path / "vault"
    home.mkdir()
    (home / "auth.json").write_text(text)
    return home


def test_it_refuses_a_vault_that_is_a_json_number(tmp_path, capsys):
    # The keeper runs unattended, nightly. Its contract is one line saying why
    # it refused, and exit 1. `5` parses, so the "cannot read" branch does not
    # catch it, and every later line assumes a mapping.
    home, out = bad_vault(tmp_path, "5"), tmp_path / "out"
    assert cli.main(["derive-credential", "--codex-home", str(home), "--out", str(out)]) == 1
    assert "reviewbot derive-credential:" in capsys.readouterr().out
    assert not (out / "auth.json").exists()


def test_it_refuses_a_vault_that_is_json_null(tmp_path, capsys):
    home, out = bad_vault(tmp_path, "null"), tmp_path / "out"
    assert cli.main(["derive-credential", "--codex-home", str(home), "--out", str(out)]) == 1
    assert "reviewbot derive-credential:" in capsys.readouterr().out


def test_it_refuses_a_vault_that_is_a_json_string(tmp_path, capsys):
    home, out = bad_vault(tmp_path, '"x"'), tmp_path / "out"
    assert cli.main(["derive-credential", "--codex-home", str(home), "--out", str(out)]) == 1
    assert "reviewbot derive-credential:" in capsys.readouterr().out


def test_it_refuses_a_vault_that_is_a_json_array(tmp_path, capsys):
    home, out = bad_vault(tmp_path, "[1, 2]"), tmp_path / "out"
    assert cli.main(["derive-credential", "--codex-home", str(home), "--out", str(out)]) == 1
    assert "reviewbot derive-credential:" in capsys.readouterr().out


def test_it_refuses_a_vault_whose_tokens_are_not_an_object(tmp_path, capsys):
    # A truthy non-mapping walks straight through `(auth.get("tokens") or {})`
    # in `access_token_expiry`, so the object guard above does not cover it.
    home, out = bad_vault(tmp_path, json.dumps({"tokens": [1, 2]})), tmp_path / "out"
    assert cli.main(["derive-credential", "--codex-home", str(home), "--out", str(out)]) == 1
    assert "reviewbot derive-credential:" in capsys.readouterr().out
    assert not (out / "auth.json").exists()


def test_a_truncated_vault_still_reports_that_it_cannot_be_read(tmp_path, capsys):
    home, out = bad_vault(tmp_path, '{"tokens": {'), tmp_path / "out"
    assert cli.main(["derive-credential", "--codex-home", str(home), "--out", str(out)]) == 1
    assert "cannot read" in capsys.readouterr().out


def test_access_token_expiry_rejects_tokens_that_are_not_an_object():
    # The documented failure mode of this function is CredentialError, and the
    # caller catches exactly that. An AttributeError escapes it.
    for tokens in ([1, 2], "x", 5):
        try:
            credentials.access_token_expiry({"tokens": tokens})
        except credentials.CredentialError:
            continue
        raise AssertionError(f"no CredentialError for tokens={tokens!r}")
