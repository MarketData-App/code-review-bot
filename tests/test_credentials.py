"""Tests for the credential library.

No test here reaches the network or holds a real token. The access tokens are
hand-built JWTs whose payload carries nothing but an `exp`.

Run: pytest tests/test_credentials.py
"""

import base64
import datetime
import json

import pytest

from reviewbot import credentials


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
