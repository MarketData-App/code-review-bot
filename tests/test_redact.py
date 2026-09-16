"""Tests for the redaction layer.

Nothing here is a real credential. Every token-shaped string is hand-built
from filler characters, and the JWTs carry an empty JSON payload.

Run: pytest tests/test_redact.py
"""

import base64
import json

from reviewbot.redact import scrub


def b64(raw: bytes) -> str:
    """Encode bytes as unpadded base64url, the way a JWT segment is encoded."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def fake_jwt() -> str:
    """A JWT-shaped string: header, payload and signature, no secret in any."""
    header = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps({"iss": "https://example.invalid", "exp": 1}).encode())
    return f"{header}.{payload}.{b64(b'not-a-real-signature-just-filler')}"


def test_a_github_token_is_still_redacted():
    assert scrub("take ghp_" + "B" * 36) == "take [redacted]"


def test_an_api_key_is_still_redacted():
    assert scrub("key sk-" + "C" * 32) == "key [redacted]"


def test_a_jwt_is_redacted():
    # This branch exists because of the borrowed Codex credential: the job now
    # writes an auth.json holding a bearer access_token onto a runner the
    # model can read, and the model's answer is echoed into the comment.
    assert scrub(f"the token is {fake_jwt()}") == "the token is [redacted]"


def test_a_jwt_inside_a_json_blob_is_redacted():
    blob = json.dumps({"tokens": {"access_token": fake_jwt(), "id_token": fake_jwt()}})
    cleaned = scrub(blob)
    assert "eyJ" not in cleaned
    assert cleaned.count("[redacted]") == 2


def test_dotted_prose_is_left_alone():
    # A pattern matching any three dotted base64url runs eats the code it is
    # quoting, and a redactor that mangles every diff gets deleted, not fixed.
    for text in [
        "reviewbot.credentials.release frees the lease",
        "docs.superpowers.specs holds the design",
        "see reviewbot/backends/codex.py and codex.exec.run",
        "version 1.2.3 of the schema",
    ]:
        assert scrub(text) == text


def test_an_empty_text_is_answered_with_an_empty_text():
    assert scrub("") == ""
    assert scrub(None) == ""
