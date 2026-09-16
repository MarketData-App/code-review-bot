"""Tests for `reviewbot credential-audit`.

The alarm a scheduled keeper workflow runs. It asks one question -- is the
copy the store publishes still good? -- and answers it with an exit code.

Two properties matter more than any single check. It must NEVER raise: a
traceback from an alarm is an alarm that did not fire. And it must catch a
keeper that has silently stopped, which the expiry check alone cannot see,
because a token published three days ago is still valid for days afterwards.

Run: pytest tests/test_audit_command.py
"""

import base64
import datetime
import json

import pytest

from reviewbot import cli, github
from tests.conftest import FakeTransport

STORE = "MarketData-App/code-review-credentials"


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


def hours_from_now(hours: float) -> str:
    return (datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=hours)).isoformat()


def meta(expires_in_hours: float = 240.0, published_hours_ago: float = 1.0) -> str:
    return json.dumps(
        {
            "expires_at": hours_from_now(expires_in_hours),
            "published_at": hours_from_now(-published_hours_ago),
        }
    )


def publish(transport, body: str) -> None:
    transport.add(
        "GET",
        f"/repos/{STORE}/contents/codex/meta.json?ref=issue",
        data={
            "encoding": "base64",
            "content": base64.b64encode(body.encode()).decode(),
            "sha": "m1",
        },
    )


def audit(*extra: str) -> int:
    return cli.main(["credential-audit", "--store", STORE, *extra])


def test_a_healthy_credential_exits_zero_and_says_so(transport, capsys):
    publish(transport, meta())
    assert audit() == 0
    out = capsys.readouterr().out
    assert "healthy=true" in out
    assert "hours_left=" in out


def test_it_reads_the_published_meta_at_the_issue_branch(transport):
    publish(transport, meta())
    audit()
    assert [c["path"] for c in transport.calls] == [
        f"/repos/{STORE}/contents/codex/meta.json?ref=issue"
    ]


def test_it_never_asks_for_the_credential_itself(transport):
    # The alarm decides from the metadata alone. Reading auth.json would put a
    # usable credential in a scheduled job that has no use for one.
    publish(transport, meta())
    audit()
    assert not any("auth.json" in call["path"] for call in transport.calls)


def test_a_token_near_expiry_is_unhealthy(transport, capsys):
    publish(transport, meta(expires_in_hours=5.0))
    assert audit("--warn-hours", "72") == 1
    out = capsys.readouterr().out
    assert "healthy=false" in out
    assert "72" in out  # the floor is named, so the log says what to fix


def test_a_stale_publish_is_unhealthy_even_with_a_young_token(transport, capsys):
    """The silent-keeper case, and the reason this command exists.

    A keeper that stopped two days ago left behind a token that is still valid
    for a week. Every expiry check passes, nothing is published, and the first
    symptom is a review failing after the token finally dies.
    """
    publish(transport, meta(expires_in_hours=240.0, published_hours_ago=72.0))
    assert audit("--stale-hours", "36") == 1
    out = capsys.readouterr().out
    assert "healthy=false" in out
    assert "36" in out


def test_a_missing_meta_is_unhealthy_and_does_not_traceback(transport, capsys):
    transport.add(
        "GET", f"/repos/{STORE}/contents/codex/meta.json?ref=issue", status=404, text="not found"
    )
    assert audit() == 1
    out = capsys.readouterr().out
    assert "healthy=false" in out
    assert "Traceback" not in out


def test_an_empty_meta_is_unhealthy(transport, capsys):
    publish(transport, "")
    assert audit() == 1
    assert "healthy=false" in capsys.readouterr().out


def test_an_unparseable_meta_is_unhealthy_and_does_not_traceback(transport, capsys):
    publish(transport, "{not json")
    assert audit() == 1
    out = capsys.readouterr().out
    assert "healthy=false" in out
    assert "Traceback" not in out


def test_meta_that_is_valid_json_but_not_an_object_is_unhealthy(transport, capsys):
    # json.loads("5") succeeds and the result has no .get, the same trap
    # credential-checkout already guards against.
    publish(transport, "5")
    assert audit() == 1
    assert "healthy=false" in capsys.readouterr().out


def test_meta_without_the_timestamps_is_unhealthy(transport, capsys):
    publish(transport, json.dumps({"expires_at": hours_from_now(240)}))
    assert audit() == 1
    assert "healthy=false" in capsys.readouterr().out


def test_an_unparseable_timestamp_is_unhealthy(transport, capsys):
    publish(transport, json.dumps({"expires_at": "soon", "published_at": "recently"}))
    assert audit() == 1
    assert "healthy=false" in capsys.readouterr().out


def _raising_transport(method, url, headers, body):
    raise OSError("network down")


def test_a_store_call_that_raises_is_unhealthy_not_a_traceback(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_" + "T" * 36)
    monkeypatch.setattr(
        cli,
        "_store_api",
        lambda repo, token: github.GitHub(
            repo, token, transport=_raising_transport, sleep=lambda s: None
        ),
    )
    assert audit() == 1
    out = capsys.readouterr().out
    assert "healthy=false" in out
    assert "Traceback" not in out


# --- the local file source --------------------------------------------------


def test_meta_file_reads_a_local_path_and_never_calls_the_store(tmp_path, transport, capsys):
    """The keeper workflow's own clone answers the question without a token.

    That workflow runs where GITHUB_TOKEN cannot read the private store, but it
    already has a deploy-key clone of it. Handing over the file is cheaper than
    minting a second credential for an alarm.
    """
    path = tmp_path / "meta.json"
    path.write_text(meta())
    assert cli.main(["credential-audit", "--meta-file", str(path)]) == 0
    assert "healthy=true" in capsys.readouterr().out
    assert transport.calls == []


def test_a_stale_local_meta_is_unhealthy(tmp_path, transport, capsys):
    path = tmp_path / "meta.json"
    path.write_text(meta(published_hours_ago=72.0))
    assert cli.main(["credential-audit", "--meta-file", str(path), "--stale-hours", "36"]) == 1
    assert "healthy=false" in capsys.readouterr().out


def test_an_unreadable_meta_file_is_unhealthy_not_a_traceback(tmp_path, transport, capsys):
    missing = tmp_path / "nothing" / "meta.json"
    assert cli.main(["credential-audit", "--meta-file", str(missing)]) == 1
    out = capsys.readouterr().out
    assert "healthy=false" in out
    assert "Traceback" not in out


# --- exactly one source -----------------------------------------------------


def test_neither_source_is_a_usage_error(transport, capsys):
    assert cli.main(["credential-audit"]) == 2
    assert "--store" in capsys.readouterr().out


def test_both_sources_are_a_usage_error(tmp_path, transport, capsys):
    path = tmp_path / "meta.json"
    path.write_text(meta())
    assert cli.main(["credential-audit", "--store", STORE, "--meta-file", str(path)]) == 2
    assert "--meta-file" in capsys.readouterr().out


# --- what the workflow step reads back --------------------------------------


def test_every_output_key_is_written_for_a_healthy_credential(tmp_path, transport, monkeypatch):
    publish(transport, meta())
    output = tmp_path / "gh-output"
    output.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert audit() == 0
    keys = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
    assert keys["healthy"] == "true"
    assert keys["expires_at"] and keys["published_at"]
    assert float(keys["hours_left"]) > 0


def test_healthy_is_written_even_when_nothing_else_can_be(tmp_path, transport, monkeypatch):
    # The step that reads `healthy` must never find the key absent, whatever
    # went wrong upstream of it.
    publish(transport, "{not json")
    output = tmp_path / "gh-output"
    output.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert audit() == 1
    lines = [ln for ln in output.read_text().splitlines() if ln.startswith("healthy=")]
    assert lines == ["healthy=false"]
