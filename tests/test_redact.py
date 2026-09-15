"""Tests for the redaction of token-shaped strings.

This is the second line of defence, never the first. It stops an accidental
echo of a credential; it cannot stop a model that deliberately encodes one.
The org-only gate (spec section 6) is what covers deliberate exfiltration.

Run: pytest tests/test_redact.py
"""

from reviewbot.redact import scrub


def test_a_github_token_is_removed():
    assert "ghs_" not in scrub("token ghs_" + "A" * 36)
    assert "ghp_" not in scrub("token ghp_" + "A" * 36)


def test_a_fine_grained_pat_is_removed():
    assert "github_pat_" not in scrub("github_pat_" + "A" * 40)


def test_an_openai_key_is_removed():
    assert "sk-" not in scrub("sk-" + "A" * 40)


def test_a_jwt_is_removed():
    # A Codex access token is a JWT. The old pattern missed it entirely.
    jwt = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9." + "a" * 40 + "." + "b" * 43
    assert jwt not in scrub(f"the token is {jwt} ok")
    assert "[redacted]" in scrub(f"the token is {jwt} ok")


def test_a_private_key_block_is_removed():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"
    out = scrub(f"here it is:\n{pem}\ndone")
    assert "MIIEow" not in out
    assert "BEGIN RSA PRIVATE KEY" not in out


def test_ordinary_prose_survives():
    text = "The retry loop never sleeps, so the backoff is discarded."
    assert scrub(text) == text


def test_code_identifiers_survive():
    text = "call reviewbot.github.GitHub._request with accept=application/vnd.github+json"
    assert scrub(text) == text


def test_a_sha_survives():
    # 40 hex characters is a commit, not a secret, and the footer prints them.
    text = "Reviewed abc1234def5678901234567890123456789abcde"
    assert scrub(text) == text


def test_none_and_empty_are_safe():
    assert scrub(None) == ""
    assert scrub("") == ""


def test_a_truncated_private_key_is_still_removed():
    # The diff cap, the 300-character error tails and the annotation limits
    # all truncate. A key block whose END marker was cut must not be echoed.
    truncated = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA" + "b" * 60
    out = scrub(f"log said: {truncated}")
    assert "MIIEowIBAAKCAQEA" not in out
    assert "[redacted]" in out


def test_the_begin_marker_alone_is_removed():
    assert "BEGIN RSA PRIVATE KEY" not in scrub("-----BEGIN RSA PRIVATE KEY-----")


def test_prose_mentioning_a_private_key_survives():
    text = "Store the private key as a secret, not in the repository."
    assert scrub(text) == text


def test_a_stray_begin_marker_does_not_swallow_the_text_after_it():
    # A bare marker appears in this repository's own test fixtures. Under
    # re.DOTALL an unbounded body ate everything to the next END marker
    # anywhere in the text: 39% of this PR's diff and nine whole files.
    text = (
        'fixture = "-----BEGIN RSA PRIVATE KEY-----"\n'
        "def test_something():\n"
        "    assert compute() == 42\n"
        "-----END RSA PRIVATE KEY-----\n"
        "def test_after():\n"
        "    assert still_here() is True\n"
    )
    out = scrub(text)
    assert "assert compute() == 42" in out
    assert "assert still_here() is True" in out
    assert "def test_something" in out


def test_a_real_key_inside_a_diff_is_still_removed():
    diff = (
        "diff --git a/secrets.pem b/secrets.pem\n"
        "--- /dev/null\n"
        "+++ b/secrets.pem\n"
        "@@ -0,0 +1,3 @@\n"
        "+-----BEGIN RSA PRIVATE KEY-----\n"
        "+MIIEowIBAAKCAQEAxLp0qS9m3nVYQb2cDfGh\n"
        "+-----END RSA PRIVATE KEY-----\n"
    )
    out = scrub(diff)
    assert "MIIEowIBAAKCAQEAxLp0qS9m3nVYQb2cDfGh" not in out
    assert "diff --git a/secrets.pem" in out


def test_scrubbing_leaves_an_ordinary_diff_untouched():
    diff = (
        "diff --git a/a.py b/a.py\n@@ -1,2 +1,2 @@\n"
        "-    return None\n+    return value\n"
        "diff --git a/b.py b/b.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    )
    assert scrub(diff) == diff


def test_a_lone_begin_marker_does_not_eat_the_prose_after_it():
    # The base64 alphabet plus \s still matched words and the spaces between
    # them, so a bare marker followed by prose ate the prose. A PEM body has
    # no spaces inside a line.
    text = "-----BEGIN RSA PRIVATE KEY----- was found in the log, says the report."
    out = scrub(text)
    assert "was found in the log" in out
    assert "says the report" in out


def test_a_multi_line_key_body_is_still_removed():
    key = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAxLp0qS9m3nVYQb2cDfGh\n"
        "ZkLmNoPqRsTuVwXyZ0123456789abcdefGH\n"
    )
    out = scrub(f"prefix {key} suffix here")
    assert "MIIEowIBAAKCAQEAxLp0qS9m3nVYQb2cDfGh" not in out
    assert "ZkLmNoPqRsTuVwXyZ0123456789abcdefGH" not in out
    assert "suffix here" in out
