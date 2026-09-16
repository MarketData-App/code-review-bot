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


def _read_lease(api) -> tuple[dict, str | None]:
    text, sha = api.file_with_sha(LEASE_PATH, LEASE_BRANCH)
    if text is None:
        return {}, None
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        # A lease nobody can parse must not wedge every review forever. Treat
        # it as free; the write below replaces it with something readable.
        return {}, sha
    return (body if isinstance(body, dict) else {}), sha


def _held(lease: dict, now: datetime.datetime) -> bool:
    if not lease.get("holder"):
        return False
    expires = lease.get("expires_at")
    if not isinstance(expires, str):
        return True
    try:
        when = datetime.datetime.fromisoformat(expires)
    except ValueError:
        return True
    return when > now


def acquire(api, holder: str, run_url: str, now: datetime.datetime, ttl_minutes: int = 20) -> bool:
    """Take the lease, or return False. Never raises for an ordinary loss.

    The lock is the contents API's own compare-and-swap: the lease is read with
    its blob sha and written back under it, so a second job racing for the same
    lease gets a 409 and loses. No lock service is involved.
    """
    lease, sha = _read_lease(api)
    if _held(lease, now):
        return False
    body = {
        "holder": holder,
        "run_url": run_url,
        "acquired_at": now.isoformat(),
        "expires_at": (now + datetime.timedelta(minutes=ttl_minutes)).isoformat(),
    }
    return api.put_file(
        LEASE_PATH, json.dumps(body, indent=2) + "\n", f"lease taken by {holder}", LEASE_BRANCH, sha
    )


def release(api, holder: str) -> None:
    """Free the lease, but only if this exact run still holds it.

    A job whose TTL expired may find another job already holding the lease by
    the time its `if: always()` step runs. Freeing it then would hand a second
    job the credential while the first is still working.

    That guarantee is only as good as `holder`, and `holder` must therefore
    name the RUN, not the pull request. The workflow sets
    `cancel-in-progress: true`, so a push to a pull request routinely leaves a
    cancelled run's check-in racing a new run that has already taken the
    lease. While the holder was `<repo>#<pr>`, those two strings were equal,
    this comparison passed, and the dying run freed the live run's lease --
    the precise case this function exists to prevent. The workflow now appends
    `#<run_id>-<run_attempt>`, so a holder identifies one run and one attempt.
    """
    lease, sha = _read_lease(api)
    if lease.get("holder") != holder:
        return
    body = {"holder": None, "run_url": None, "acquired_at": None, "expires_at": None}
    api.put_file(
        LEASE_PATH, json.dumps(body, indent=2) + "\n", f"lease freed by {holder}", LEASE_BRANCH, sha
    )
