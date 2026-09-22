"""Tests for the credential library.

No test here reaches the network or holds a real token. The access tokens are
hand-built JWTs whose payload carries nothing but an `exp`.

Run: pytest tests/test_credentials.py
"""

import base64
import datetime
import json

import pytest

from reviewbot import credentials, github
from tests.conftest import FakeTransport


def b64(raw: bytes) -> str:
    """Encode bytes as unpadded base64."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwt_with_exp(exp: int) -> str:
    """A JWT-shaped string. Only the payload's `exp` is ever read."""
    return f"{b64(b'{}')}.{b64(json.dumps({'exp': exp}).encode())}.{b64(b'signature')}"


def auth_file(exp: int = 2_000_000_000) -> dict:
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": jwt_with_exp(exp),
            "access_token": jwt_with_exp(exp),
            "refresh_token": "a-real-looking-refresh-token",
            "account_id": "11111111-2222-3333-4444-555555555555",
        },
        "last_refresh": "2026-09-08T00:17:16.736282269Z",
    }


def test_derive_replaces_the_refresh_token_with_the_placeholder():
    out = credentials.derive(auth_file())
    assert out["tokens"]["refresh_token"] == credentials.PLACEHOLDER
    assert credentials.PLACEHOLDER == "REVIEWBOT-PLACEHOLDER-NOT-A-REFRESH-TOKEN"


def test_derive_changes_nothing_else():
    source = auth_file()
    out = credentials.derive(source)
    for key in ("auth_mode", "OPENAI_API_KEY", "last_refresh"):
        assert out[key] == source[key]
    for key in ("id_token", "access_token", "account_id"):
        assert out["tokens"][key] == source["tokens"][key]


def test_derive_does_not_mutate_its_argument():
    # The keeper reads the vault and derives from it in one process. A mutating
    # derive would write the placeholder back into the vault and destroy it.
    source = auth_file()
    credentials.derive(source)
    assert source["tokens"]["refresh_token"] == "a-real-looking-refresh-token"


def test_derive_refuses_a_file_with_no_refresh_token():
    source = auth_file()
    del source["tokens"]["refresh_token"]
    with pytest.raises(credentials.CredentialError, match="refresh_token"):
        credentials.derive(source)


def test_the_expiry_is_read_from_the_access_token():
    when = credentials.access_token_expiry(auth_file(exp=1_800_000_000))
    assert when == datetime.datetime(2027, 1, 15, 8, 0, tzinfo=datetime.UTC)


def test_a_malformed_access_token_raises_rather_than_reading_as_far_future():
    # Returning a far-future date here would let the keeper publish a token it
    # could not read the expiry of, which is the one thing it exists to check.
    source = auth_file()
    source["tokens"]["access_token"] = "not-a-jwt"
    with pytest.raises(credentials.CredentialError, match="access_token"):
        credentials.access_token_expiry(source)


# --- the lease -------------------------------------------------------------

NOW = datetime.datetime(2026, 9, 16, 14, 0, tzinfo=datetime.UTC)
STORE = "MarketData-App/code-review-credentials"


@pytest.fixture
def store():
    transport = FakeTransport()
    api = github.GitHub(STORE, "ghs_" + "T" * 36, transport=transport, sleep=lambda s: None)
    return api, transport


def lease_body(transport, holder, expires_at, sha="blob111"):
    transport.add(
        "GET",
        f"/repos/{STORE}/contents/codex/lease.json?ref=main",
        data={
            "encoding": "base64",
            "content": base64.b64encode(
                json.dumps({"holder": holder, "expires_at": expires_at}).encode()
            ).decode(),
            "sha": sha,
        },
    )


def corrupt_lease_body(transport, sha="blob111"):
    transport.add(
        "GET",
        f"/repos/{STORE}/contents/codex/lease.json?ref=main",
        data={
            "encoding": "base64",
            "content": base64.b64encode(b"not json at all").decode(),
            "sha": sha,
        },
    )


def test_a_free_lease_is_acquired(store):
    api, transport = store
    lease_body(transport, None, None)
    transport.add("PUT", f"/repos/{STORE}/contents/codex/lease.json", data={"commit": {}})
    assert credentials.acquire(api, "sdk-py#100", "https://run/1", NOW) is True
    written = json.loads(base64.b64decode(transport.calls[-1]["body"]["content"]))
    assert written["holder"] == "sdk-py#100"
    assert written["expires_at"] == "2026-09-16T14:20:00+00:00"


def test_a_lease_held_by_another_job_is_refused(store):
    api, transport = store
    lease_body(transport, "sdk-go#41", "2026-09-16T14:10:00+00:00")
    assert credentials.acquire(api, "sdk-py#100", "https://run/1", NOW) is False
    assert [c["method"] for c in transport.calls] == ["GET"]


def test_an_expired_lease_is_taken_over(store):
    # This is what makes a cancelled job harmless: it leaves the lease held,
    # and the next job simply takes it when the TTL has passed.
    api, transport = store
    lease_body(transport, "sdk-go#41", "2026-09-16T13:59:00+00:00")
    transport.add("PUT", f"/repos/{STORE}/contents/codex/lease.json", data={"commit": {}})
    assert credentials.acquire(api, "sdk-py#100", "https://run/1", NOW) is True


def test_a_missing_lease_file_is_created(store):
    api, transport = store
    transport.add(
        "GET", f"/repos/{STORE}/contents/codex/lease.json?ref=main", status=404, text="Not Found"
    )
    transport.add("PUT", f"/repos/{STORE}/contents/codex/lease.json", data={"commit": {}})
    assert credentials.acquire(api, "sdk-py#100", "https://run/1", NOW) is True
    assert "sha" not in transport.calls[-1]["body"]


def test_a_corrupt_lease_body_is_treated_as_free(store):
    # A lease nobody can parse must not wedge every review forever, so
    # `_read_lease` treats it as free rather than raising or refusing.
    api, transport = store
    corrupt_lease_body(transport)
    transport.add("PUT", f"/repos/{STORE}/contents/codex/lease.json", data={"commit": {}})
    assert credentials.acquire(api, "sdk-py#100", "https://run/1", NOW) is True


def test_losing_the_compare_and_swap_is_not_acquiring(store):
    api, transport = store
    lease_body(transport, None, None)
    transport.add(
        "PUT", f"/repos/{STORE}/contents/codex/lease.json", status=409, text="does not match"
    )
    assert credentials.acquire(api, "sdk-py#100", "https://run/1", NOW) is False


def test_release_frees_the_lease(store):
    api, transport = store
    lease_body(transport, "sdk-py#100", "2026-09-16T14:20:00+00:00")
    transport.add("PUT", f"/repos/{STORE}/contents/codex/lease.json", data={"commit": {}})
    credentials.release(api, "sdk-py#100")
    written = json.loads(base64.b64decode(transport.calls[-1]["body"]["content"]))
    assert written["holder"] is None


def test_release_leaves_a_lease_another_job_now_holds(store):
    # Our TTL expired, another job took the lease, and only then did our
    # `if: always()` step run. Freeing it here would hand a second job the
    # credential while the first is still using it.
    api, transport = store
    lease_body(transport, "sdk-go#41", "2026-09-16T14:30:00+00:00")
    credentials.release(api, "sdk-py#100")
    assert [c["method"] for c in transport.calls] == ["GET"]


# --- fix round 2: an unreadable lease must not wedge every review ----------


def test_a_lease_with_no_expiry_is_taken_over(store):
    # `expires_at` absent used to read as "held forever", so one bad write
    # into the store stopped Codex on every repository until a human edited
    # the file by hand. The design promises no human action and no recovery
    # runbook, so an expiry that cannot be read means free.
    api, transport = store
    transport.add(
        "GET",
        f"/repos/{STORE}/contents/codex/lease.json?ref=main",
        data={
            "encoding": "base64",
            "content": base64.b64encode(json.dumps({"holder": "sdk-go#41"}).encode()).decode(),
            "sha": "blob111",
        },
    )
    transport.add("PUT", f"/repos/{STORE}/contents/codex/lease.json", data={"commit": {}})
    assert credentials.acquire(api, "sdk-py#100#7", "https://run/1", NOW) is True


def test_a_lease_with_an_unparseable_expiry_is_taken_over(store):
    api, transport = store
    lease_body(transport, "sdk-go#41", "next tuesday")
    transport.add("PUT", f"/repos/{STORE}/contents/codex/lease.json", data={"commit": {}})
    assert credentials.acquire(api, "sdk-py#100#7", "https://run/1", NOW) is True


def test_a_lease_with_an_expiry_of_the_wrong_type_is_taken_over(store):
    api, transport = store
    lease_body(transport, "sdk-go#41", 1_800_000_000)
    transport.add("PUT", f"/repos/{STORE}/contents/codex/lease.json", data={"commit": {}})
    assert credentials.acquire(api, "sdk-py#100#7", "https://run/1", NOW) is True


def test_a_naive_expiry_is_compared_rather_than_raising(store):
    # Comparing a naive datetime with an aware one raises TypeError, which
    # escaped `acquire` and failed the borrow step. A naive stamp is read as
    # UTC: the bot only ever writes aware UTC, so a naive one is a hand edit.
    api, transport = store
    lease_body(transport, "sdk-go#41", "2026-09-16T14:10:00")
    assert credentials.acquire(api, "sdk-py#100#7", "https://run/1", NOW) is False
    assert [c["method"] for c in transport.calls] == ["GET"]


def test_a_naive_expiry_in_the_past_frees_the_lease(store):
    api, transport = store
    lease_body(transport, "sdk-go#41", "2026-09-16T13:59:00")
    transport.add("PUT", f"/repos/{STORE}/contents/codex/lease.json", data={"commit": {}})
    assert credentials.acquire(api, "sdk-py#100#7", "https://run/1", NOW) is True


# --- fix round 2: the holder names one run, not one pull request ----------


def test_two_runs_of_one_pull_request_do_not_free_each_other(store):
    # cancel-in-progress kills the first run on every push, and its `always()`
    # check-in can land after the replacement run has taken the lease. While
    # the holder was `<repo>#<pr>` the two strings matched, so the dying run
    # freed the live run's lease -- the case this guard exists to prevent.
    api, transport = store
    lease_body(transport, "MarketData-App/sdk-py#100#552-1", "2026-09-16T14:20:00+00:00")
    credentials.release(api, "MarketData-App/sdk-py#100#551-1")
    assert [c["method"] for c in transport.calls] == ["GET"]


def test_a_run_still_frees_its_own_lease(store):
    api, transport = store
    lease_body(transport, "MarketData-App/sdk-py#100#551-1", "2026-09-16T14:20:00+00:00")
    transport.add("PUT", f"/repos/{STORE}/contents/codex/lease.json", data={"commit": {}})
    credentials.release(api, "MarketData-App/sdk-py#100#551-1")
    written = json.loads(base64.b64decode(transport.calls[-1]["body"]["content"]))
    assert written["holder"] is None


# --- the commit message must not reference the pull request ----------------
#
# `holder` is `<owner>/<repo>#<pr>#<run>-<attempt>`, and `<owner>/<repo>#<pr>`
# is GitHub's CROSS-REPOSITORY ISSUE REFERENCE syntax. Putting it in a commit
# message posted a "referenced this in code-review-credentials" event onto the
# pull request's timeline -- twice per review, on every review, forever.
# Measured 2026-09-22 on MarketData-App/api#463: twelve lease commits, twelve
# timeline events, timestamps matching to the second.
#
# The holder itself is unchanged: `release` compares it to decide whether this
# run still owns the lease, and that identity is why it carries the run id.
# Only the human-facing message is rewritten.

HOLDER = "MarketData-App/api#463#35743856939-1"


def _message_of(transport):
    return transport.calls[-1]["body"]["message"]


def test_acquire_does_not_write_a_cross_repository_reference(store):
    api, transport = store
    lease_body(transport, None, None)
    transport.add("PUT", f"/repos/{STORE}/contents/codex/lease.json", data={"commit": {}})
    credentials.acquire(api, HOLDER, "https://run/1", NOW)
    assert "#" not in _message_of(transport)
    assert "api#463" not in _message_of(transport)


def test_release_does_not_write_a_cross_repository_reference(store):
    api, transport = store
    lease_body(transport, HOLDER, "2026-09-16T14:20:00+00:00")
    transport.add("PUT", f"/repos/{STORE}/contents/codex/lease.json", data={"commit": {}})
    credentials.release(api, HOLDER)
    assert "#" not in _message_of(transport)


def test_the_message_still_names_the_repository_pull_request_and_run(store):
    api, transport = store
    lease_body(transport, None, None)
    transport.add("PUT", f"/repos/{STORE}/contents/codex/lease.json", data={"commit": {}})
    credentials.acquire(api, HOLDER, "https://run/1", NOW)
    message = _message_of(transport)
    assert message == "lease taken by MarketData-App/api pull 463 run 35743856939-1"


def test_the_lease_body_still_carries_the_holder_unchanged(store):
    # The identity `release` compares. Changing it would reopen the race that
    # `#<run_id>-<run_attempt>` was added to close.
    api, transport = store
    lease_body(transport, None, None)
    transport.add("PUT", f"/repos/{STORE}/contents/codex/lease.json", data={"commit": {}})
    credentials.acquire(api, HOLDER, "https://run/1", NOW)
    written = json.loads(base64.b64decode(transport.calls[-1]["body"]["content"]))
    assert written["holder"] == HOLDER


def test_a_holder_with_no_pull_request_still_gets_a_message(store):
    api, transport = store
    lease_body(transport, None, None)
    transport.add("PUT", f"/repos/{STORE}/contents/codex/lease.json", data={"commit": {}})
    credentials.acquire(api, "verification", "https://run/1", NOW)
    assert _message_of(transport) == "lease taken by verification"
