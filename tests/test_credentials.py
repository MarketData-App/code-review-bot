"""Tests for the credential library.

No test here reaches the network or holds a real token. The access tokens are
hand-built JWTs whose payload carries nothing but an `exp`.

Run: pytest tests/test_credentials.py
"""

import base64
import datetime
import json

import pytest
from conftest import FakeTransport

from reviewbot import credentials, github


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
