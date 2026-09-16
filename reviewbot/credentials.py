"""The Codex credential: derive a copy that cannot refresh, and lease it.

One real `auth.json` lives in a vault on skynet and never moves. What every
review job borrows is a DERIVED copy whose refresh token is a placeholder.
That is the whole security argument: OpenAI's guidance forbids sharing one
`auth.json` across concurrent jobs because two processes redeeming one refresh
token kills the credential (`refresh_token_reused`). A copy that holds no
refresh token cannot redeem one, so no number of concurrent jobs can cause it.

Measured 2026-09-16 with Codex CLI 0.153.4: a hand-written `auth.json` whose
`refresh_token` is a placeholder runs `codex exec` to completion, and does so
even with `last_refresh` 46 days old. `refresh_token` must be PRESENT, because
Codex fails to parse the file without it; it need not be real.
"""

import base64
import binascii
import copy
import datetime
import json

PLACEHOLDER = "REVIEWBOT-PLACEHOLDER-NOT-A-REFRESH-TOKEN"

ISSUE_PATH = "codex/auth.json"
META_PATH = "codex/meta.json"
LEASE_PATH = "codex/lease.json"

ISSUE_BRANCH = "issue"
LEASE_BRANCH = "main"


class CredentialError(RuntimeError):
    """The credential could not be read, derived or leased."""


def derive(auth: dict) -> dict:
    """A copy of `auth` that authenticates but cannot refresh.

    Deep-copied, because the keeper derives from the vault it has just read and
    a mutating version would write the placeholder back into the vault.
    """
    tokens = auth.get("tokens")
    if not isinstance(tokens, dict) or not tokens.get("refresh_token"):
        raise CredentialError("the credential has no refresh_token to replace")
    out = copy.deepcopy(auth)
    out["tokens"]["refresh_token"] = PLACEHOLDER
    return out


def access_token_expiry(auth: dict) -> datetime.datetime:
    """When the access token stops working, as an aware UTC datetime.

    The token is never verified here, only read: this decides whether the
    keeper may publish, and GitHub is not the issuer.
    """
    token = (auth.get("tokens") or {}).get("access_token")
    if not isinstance(token, str):
        raise CredentialError("the credential has no access_token")
    parts = token.split(".")
    if len(parts) != 3:
        raise CredentialError("the access_token is not a JWT")
    segment = parts[1]
    try:
        payload = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise CredentialError(f"the access_token payload could not be read: {exc}") from exc
    exp = payload.get("exp")
    if not isinstance(exp, int):
        raise CredentialError("the access_token carries no exp claim")
    return datetime.datetime.fromtimestamp(exp, datetime.UTC)
