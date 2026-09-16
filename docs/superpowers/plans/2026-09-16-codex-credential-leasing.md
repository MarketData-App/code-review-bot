# Codex credential leasing — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let every repository the bot reviews — public ones included — run the
Codex backend from a dedicated personal ChatGPT plan, by lending each job a
derived `auth.json` that cannot refresh anything.

**Architecture:** One real credential lives in a vault on skynet and never
moves. A keeper derives a copy whose refresh token is a placeholder, and
publishes it to a private store repository. Each job checks that copy out
under a compare-and-swap lease, writes it to a job-scoped `CODEX_HOME`,
reviews, then deletes the file and releases the lease. Because no job holds a
refresh token, `refresh_token_reused` cannot occur and a cancelled job strands
nothing.

**Tech Stack:** Python 3.12, uv, pytest, ruff. No new runtime dependency: JWT
claims are read with `base64` and `json`, not a JWT library, because only the
unsigned `exp` claim is needed and the token is never verified locally.

**Spec:** `docs/superpowers/specs/2026-09-16-codex-credential-leasing-design.md`

## Global Constraints

- **The default test run reaches no network, starts no model, and needs no
  GitHub token.** Every test here uses the injected `FakeTransport` from
  `tests/test_github.py` or a fake CLI on PATH via `tests/conftest.py`.
- **Python 3.12**, `requires-python = ">=3.12"`. Line length 100 (`ruff`).
- **No new dependency.** The project depends on `jsonschema`, `pyyaml`,
  `requests` and nothing else.
- **No secret in git, ever.** No test fixture may contain a real token. Use the
  literal strings given in this plan.
- **The placeholder string is exactly** `REVIEWBOT-PLACEHOLDER-NOT-A-REFRESH-TOKEN`.
  It is asserted in tests and read by a human in an incident; do not reword it.
- **Store paths are exactly** `codex/auth.json` and `codex/meta.json` on branch
  `issue`, and `codex/lease.json` on branch `main`.
- **Lease TTL is 20 minutes.**
- **Commit style:** the repository's log uses `type: lower-case sentence saying
  what changed and why`, e.g. `fix: the gate and the run decided trust from
  different evidence`.

## Out of scope for this plan

The host work lives in `MarketData-App/self-hosted-runner` and needs its own
plan there: creating the `codex-keeper` tenant, its rootless daemon, the vault
volume, the keeper container, the systemd timer that calls
`reviewbot derive-credential`, the `git` force-push of the `issue` branch, and
adding `claude` and `codex` to the runner image. Task 7 here leaves the
workflow inert until that plan lands.

---

### Task 1: Derive a credential that cannot refresh

**Files:**
- Create: `reviewbot/credentials.py`
- Test: `tests/test_credentials.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `PLACEHOLDER: str`, `ISSUE_PATH`, `META_PATH`, `LEASE_PATH`,
  `CredentialError(RuntimeError)`, `derive(auth: dict) -> dict`,
  `access_token_expiry(auth: dict) -> datetime` (timezone-aware, UTC).

- [ ] **Step 1: Write the failing test**

```python
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


def jwt_with_exp(exp: int) -> str:
    """A JWT-shaped string. Only the payload's `exp` is ever read."""
    b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_credentials.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'reviewbot.credentials'`

- [ ] **Step 3: Write minimal implementation**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_credentials.py -v && uv run ruff check . && uv run ruff format --check .`
Expected: all PASS, ruff clean

- [ ] **Step 5: Commit**

```bash
git add reviewbot/credentials.py tests/test_credentials.py
git commit -m "feat: a derived credential authenticates but cannot refresh"
```

---

### Task 2: Read and write a file in another repository

**Files:**
- Modify: `reviewbot/github.py` (add two methods after `file_at_ref`, near line 165)
- Test: `tests/test_github.py` (append a section)

**Interfaces:**
- Consumes: `GitHub`, `GitHubError`, `Response` from Task 0 (existing code).
- Produces: `GitHub.file_with_sha(path: str, ref: str) -> tuple[str | None, str | None]`
  returning `(text, blob_sha)`, and
  `GitHub.put_file(path: str, text: str, message: str, branch: str, sha: str | None) -> bool`
  returning `False` when GitHub answers 409, `True` on success.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_github.py`:

```python
# --- reading and writing one file, with its blob sha ------------------------


def test_file_with_sha_returns_the_text_and_the_blob_sha(api, transport):
    import base64

    transport.add(
        "GET",
        "/repos/MarketData-App/api/contents/codex/lease.json?ref=main",
        data={
            "encoding": "base64",
            "content": base64.b64encode(b'{"holder": null}').decode(),
            "sha": "blob111",
        },
    )
    text, sha = api.file_with_sha("codex/lease.json", "main")
    assert text == '{"holder": null}'
    assert sha == "blob111"


def test_file_with_sha_reads_a_missing_file_as_two_nones(api, transport):
    transport.add(
        "GET", "/repos/MarketData-App/api/contents/codex/lease.json?ref=main", status=404,
        text="Not Found",
    )
    assert api.file_with_sha("codex/lease.json", "main") == (None, None)


def test_put_file_sends_the_branch_and_the_sha(api, transport):
    import base64

    transport.add("PUT", "/repos/MarketData-App/api/contents/codex/lease.json", data={"commit": {}})
    assert api.put_file("codex/lease.json", "{}", "take the lease", "main", "blob111") is True
    sent = transport.calls[-1]["body"]
    assert sent["branch"] == "main"
    assert sent["sha"] == "blob111"
    assert sent["message"] == "take the lease"
    assert base64.b64decode(sent["content"]).decode() == "{}"


def test_put_file_omits_the_sha_when_creating_a_new_file(api, transport):
    transport.add("PUT", "/repos/MarketData-App/api/contents/codex/lease.json", data={"commit": {}})
    api.put_file("codex/lease.json", "{}", "create the lease", "main", None)
    assert "sha" not in transport.calls[-1]["body"]


def test_put_file_reads_a_409_as_lost_the_race_not_as_an_error(api, transport):
    # The contents API answers 409 when the blob sha no longer matches. That is
    # the compare-and-swap working, and it is how two jobs racing for the lease
    # produce one winner. It must not raise.
    transport.add(
        "PUT", "/repos/MarketData-App/api/contents/codex/lease.json", status=409,
        text="does not match",
    )
    assert api.put_file("codex/lease.json", "{}", "take the lease", "main", "stale") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_github.py -k "file_with_sha or put_file" -v`
Expected: FAIL with `AttributeError: 'GitHub' object has no attribute 'file_with_sha'`

- [ ] **Step 3: Write minimal implementation**

Insert into `reviewbot/github.py`, immediately after `file_at_ref`:

```python
    def file_with_sha(self, path: str, ref: str) -> tuple[str | None, str | None]:
        """A file's text and its blob sha at `ref`, or (None, None).

        The sha is what makes `put_file` a compare-and-swap, so it is returned
        beside the text rather than fetched again.
        """
        import base64

        quoted = urllib.parse.quote(path)
        try:
            reply = self._request(
                "GET", f"/repos/{self.repo}/contents/{quoted}?ref={urllib.parse.quote(ref)}"
            )
        except GitHubError as exc:
            if " 404" in str(exc):
                return None, None
            raise
        data = reply.data or {}
        if data.get("encoding") != "base64" or "content" not in data:
            return None, None
        return base64.b64decode(data["content"]).decode("utf-8"), data.get("sha")

    def put_file(
        self, path: str, text: str, message: str, branch: str, sha: str | None
    ) -> bool:
        """Write one file. False means GitHub answered 409: someone else won.

        A 409 is not a failure here. The contents API rejects a write whose
        `sha` no longer matches the blob it was read from, and that rejection
        is the whole lock: two jobs racing for the lease produce one 200 and
        one 409, with no lock service anywhere.
        """
        import base64

        quoted = urllib.parse.quote(path)
        body = {
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        try:
            self._request("PUT", f"/repos/{self.repo}/contents/{quoted}", body=body)
        except GitHubError as exc:
            if " 409" in str(exc):
                return False
            raise
        return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_github.py -v && uv run ruff check .`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add reviewbot/github.py tests/test_github.py
git commit -m "feat: read a file with its blob sha, and write it back under that sha"
```

---

### Task 3: The lease

**Files:**
- Modify: `reviewbot/credentials.py`
- Test: `tests/test_credentials.py`

**Interfaces:**
- Consumes: `GitHub.file_with_sha`, `GitHub.put_file` (Task 2); `LEASE_PATH`,
  `LEASE_BRANCH`, `CredentialError` (Task 1).
- Produces: `acquire(api, holder: str, run_url: str, now: datetime, ttl_minutes: int = 20) -> bool`
  and `release(api, holder: str) -> None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_credentials.py`:

```python
# --- the lease -------------------------------------------------------------

import base64 as _b64

from reviewbot import github

NOW = datetime.datetime(2026, 9, 16, 14, 0, tzinfo=datetime.UTC)
STORE = "MarketData-App/code-review-credentials"


class FakeTransport:
    """Answers by (method, path); records calls. Mirrors tests/test_github.py."""

    def __init__(self):
        self.routes, self.calls = {}, []

    def add(self, method, path, status=200, data=None, text=""):
        self.routes.setdefault((method, path), []).append(
            github.Response(status=status, data=data, text=text)
        )

    def __call__(self, method, url, headers, body):
        path = url.replace("https://api.github.com", "")
        self.calls.append({"method": method, "path": path, "body": json.loads(body) if body else None})
        queue = self.routes.get((method, path))
        if not queue:
            raise AssertionError(f"no fake route for {method} {path}")
        return queue.pop(0) if len(queue) > 1 else queue[0]


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
            "content": _b64.b64encode(
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
    written = json.loads(_b64.b64decode(transport.calls[-1]["body"]["content"]))
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
    transport.add("GET", f"/repos/{STORE}/contents/codex/lease.json?ref=main", status=404, text="Not Found")
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
    written = json.loads(_b64.b64decode(transport.calls[-1]["body"]["content"]))
    assert written["holder"] is None


def test_release_leaves_a_lease_another_job_now_holds(store):
    # Our TTL expired, another job took the lease, and only then did our
    # `if: always()` step run. Freeing it here would hand a second job the
    # credential while the first is still using it.
    api, transport = store
    lease_body(transport, "sdk-go#41", "2026-09-16T14:30:00+00:00")
    credentials.release(api, "sdk-py#100")
    assert [c["method"] for c in transport.calls] == ["GET"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_credentials.py -k "lease or acquir or release" -v`
Expected: FAIL with `AttributeError: module 'reviewbot.credentials' has no attribute 'acquire'`

- [ ] **Step 3: Write minimal implementation**

Append to `reviewbot/credentials.py`:

```python
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


def acquire(
    api, holder: str, run_url: str, now: datetime.datetime, ttl_minutes: int = 20
) -> bool:
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
    """Free the lease, but only if we still hold it.

    A job whose TTL expired may find another job already holding the lease by
    the time its `if: always()` step runs. Freeing it then would hand a second
    job the credential while the first is still working.
    """
    lease, sha = _read_lease(api)
    if lease.get("holder") != holder:
        return
    body = {"holder": None, "run_url": None, "acquired_at": None, "expires_at": None}
    api.put_file(
        LEASE_PATH, json.dumps(body, indent=2) + "\n", f"lease freed by {holder}", LEASE_BRANCH, sha
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_credentials.py -v && uv run ruff check .`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add reviewbot/credentials.py tests/test_credentials.py
git commit -m "feat: lease the credential with the contents API's compare-and-swap"
```

---

### Task 4: `reviewbot derive-credential`, the keeper's half that lives here

**Files:**
- Modify: `reviewbot/cli.py` (add a subparser in `main`, near line 415; add a function above `main`)
- Test: `tests/test_derive_command.py`

**Interfaces:**
- Consumes: `derive`, `access_token_expiry`, `CredentialError` (Task 1).
- Produces: CLI `reviewbot derive-credential --codex-home DIR --out DIR [--min-hours 48]`.
  Exit 0 on publish, exit 1 when the token expires too soon or cannot be read.
  Writes `<out>/auth.json` (mode 600) and `<out>/meta.json`.

- [ ] **Step 1: Write the failing test**

```python
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


def jwt_with_exp(exp: int) -> str:
    b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return f"{b64(b'{}')}.{b64(json.dumps({'exp': exp}).encode())}.{b64(b'sig')}"


def vault(tmp_path, hours_left: float):
    exp = int((datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=hours_left)).timestamp())
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


def test_it_refuses_to_publish_a_token_that_expires_too_soon(tmp_path, capsys):
    # Publishing a credential that dies mid-review is worse than publishing
    # none: a missing copy degrades to a Claude-only review, an expiring one
    # fails in the middle of a job.
    home, out = vault(tmp_path, hours_left=12), tmp_path / "out"
    assert cli.main(["derive-credential", "--codex-home", str(home), "--out", str(out)]) == 1
    assert not (out / "auth.json").exists()
    assert "expires" in capsys.readouterr().out


def test_it_refuses_when_there_is_no_vault(tmp_path, capsys):
    assert cli.main(
        ["derive-credential", "--codex-home", str(tmp_path / "nothing"), "--out", str(tmp_path / "out")]
    ) == 1
    assert "auth.json" in capsys.readouterr().out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_derive_command.py -v`
Expected: FAIL with `SystemExit: 2` — `argument command: invalid choice: 'derive-credential'`

- [ ] **Step 3: Write minimal implementation**

Add above `def main(` in `reviewbot/cli.py`:

```python
def derive_credential(codex_home: str, out: str, min_hours: float) -> int:
    """Write the copy every review job borrows. Runs on skynet, in the keeper.

    It refuses rather than publishing a credential that will expire during a
    review: a missing copy degrades to a Claude-only review, and an expiring
    one fails in the middle of a job.
    """
    import datetime as _dt
    from pathlib import Path

    from reviewbot import credentials

    source = Path(codex_home) / "auth.json"
    try:
        auth = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"reviewbot derive-credential: cannot read {source}: {exc}")
        return 1

    try:
        expires = credentials.access_token_expiry(auth)
        copy_out = credentials.derive(auth)
    except credentials.CredentialError as exc:
        print(f"reviewbot derive-credential: {exc}")
        return 1

    now = _dt.datetime.now(_dt.UTC)
    left = (expires - now).total_seconds() / 3600
    if left < min_hours:
        print(
            f"reviewbot derive-credential: refusing to publish, the access token "
            f"expires in {left:.1f}h ({expires.isoformat()}), under the {min_hours}h floor"
        )
        return 1

    target = Path(out)
    target.mkdir(parents=True, exist_ok=True)
    auth_path = target / "auth.json"
    auth_path.write_text(json.dumps(copy_out))
    auth_path.chmod(0o600)
    (target / "meta.json").write_text(
        json.dumps(
            {"expires_at": expires.isoformat(), "published_at": now.isoformat()}, indent=2
        )
        + "\n"
    )
    print(f"reviewbot derive-credential: published, the access token expires {expires.isoformat()}")
    return 0
```

Add inside `main`, after the `gater` subparser block:

```python
    deriver = sub.add_parser(
        "derive-credential", help="write the derived copy the review jobs borrow"
    )
    deriver.add_argument("--codex-home", required=True, help="the vault's CODEX_HOME")
    deriver.add_argument("--out", required=True, help="where to write auth.json and meta.json")
    deriver.add_argument(
        "--min-hours",
        type=float,
        default=48.0,
        help="refuse to publish a token with less life than this",
    )
```

Add inside `main`, immediately after `args = parser.parse_args(argv)` and **before**
the `token = os.environ.get(...)` line, because this command needs no GitHub token:

```python
    if args.command == "derive-credential":
        return derive_credential(args.codex_home, args.out, args.min_hours)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_derive_command.py -v && uv run pytest && uv run ruff check .`
Expected: all PASS. The full run must stay green — `main` now returns before the
token check for this one command, and the existing CLI tests assert that check.

- [ ] **Step 5: Commit**

```bash
git add reviewbot/cli.py tests/test_derive_command.py
git commit -m "feat: derive-credential writes the copy the review jobs borrow"
```

---

### Task 5: `reviewbot credential-checkout` and `credential-checkin`

**Files:**
- Modify: `reviewbot/cli.py`
- Test: `tests/test_checkout_command.py`

**Interfaces:**
- Consumes: `acquire`, `release`, `ISSUE_PATH`, `ISSUE_BRANCH` (Tasks 1 and 3);
  `GitHub.file_at_ref` (existing).
- Produces: CLI `reviewbot credential-checkout --store REPO --holder S --run-url U --codex-home DIR`
  (exit 0 always; prints `fetched=true|false` and writes it to `$GITHUB_OUTPUT`)
  and `reviewbot credential-checkin --store REPO --holder S --codex-home DIR`.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the check-out and check-in commands.

Neither may fail a review. A credential the job cannot borrow means the Codex
backend is skipped and the review runs with Claude, which is what
`backends.run()` already does with a backend that cannot start.

Run: pytest tests/test_checkout_command.py
"""

import base64
import json
import stat

import pytest

from reviewbot import cli, credentials, github

STORE = "MarketData-App/code-review-credentials"
ISSUE = {"auth_mode": "chatgpt", "tokens": {"access_token": "AAA", "refresh_token": credentials.PLACEHOLDER}}


class FakeTransport:
    def __init__(self):
        self.routes, self.calls = {}, []

    def add(self, method, path, status=200, data=None, text=""):
        self.routes.setdefault((method, path), []).append(
            github.Response(status=status, data=data, text=text)
        )

    def __call__(self, method, url, headers, body):
        path = url.replace("https://api.github.com", "")
        self.calls.append({"method": method, "path": path, "body": json.loads(body) if body else None})
        queue = self.routes.get((method, path))
        if not queue:
            raise AssertionError(f"no fake route for {method} {path}")
        return queue.pop(0) if len(queue) > 1 else queue[0]


@pytest.fixture
def transport(monkeypatch):
    fake = FakeTransport()
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_" + "T" * 36)
    monkeypatch.setattr(
        cli, "_store_api", lambda repo, token: github.GitHub(repo, token, transport=fake, sleep=lambda s: None)
    )
    return fake


def free_lease(transport):
    transport.add(
        "GET",
        f"/repos/{STORE}/contents/codex/lease.json?ref=main",
        data={"encoding": "base64", "content": base64.b64encode(b'{"holder": null}').decode(), "sha": "b1"},
    )
    transport.add("PUT", f"/repos/{STORE}/contents/codex/lease.json", data={"commit": {}})


def issue_copy(transport):
    transport.add(
        "GET",
        f"/repos/{STORE}/contents/codex/auth.json?ref=issue",
        data={"encoding": "base64", "content": base64.b64encode(json.dumps(ISSUE).encode()).decode(), "sha": "b2"},
    )


def test_checkout_writes_the_credential_and_reports_fetched(tmp_path, transport, capsys):
    free_lease(transport)
    issue_copy(transport)
    home = tmp_path / "codex-home"
    code = cli.main(
        ["credential-checkout", "--store", STORE, "--holder", "sdk-py#100",
         "--run-url", "https://run/1", "--codex-home", str(home)]
    )
    assert code == 0
    assert json.loads((home / "auth.json").read_text())["tokens"]["refresh_token"] == credentials.PLACEHOLDER
    assert stat.S_IMODE((home / "auth.json").stat().st_mode) == 0o600
    assert "fetched=true" in capsys.readouterr().out


def test_checkout_masks_every_token_value_in_the_log(tmp_path, transport, capsys):
    free_lease(transport)
    issue_copy(transport)
    cli.main(
        ["credential-checkout", "--store", STORE, "--holder", "sdk-py#100",
         "--run-url", "https://run/1", "--codex-home", str(tmp_path / "h")]
    )
    assert "::add-mask::AAA" in capsys.readouterr().out


def test_a_held_lease_reports_not_fetched_and_still_exits_zero(tmp_path, transport, capsys):
    transport.add(
        "GET",
        f"/repos/{STORE}/contents/codex/lease.json?ref=main",
        data={
            "encoding": "base64",
            "content": base64.b64encode(
                json.dumps({"holder": "sdk-go#41", "expires_at": "2099-01-01T00:00:00+00:00"}).encode()
            ).decode(),
            "sha": "b1",
        },
    )
    home = tmp_path / "codex-home"
    assert cli.main(
        ["credential-checkout", "--store", STORE, "--holder", "sdk-py#100",
         "--run-url", "https://run/1", "--codex-home", str(home), "--attempts", "1"]
    ) == 0
    assert not (home / "auth.json").exists()
    assert "fetched=false" in capsys.readouterr().out


def test_an_unreachable_store_reports_not_fetched_and_still_exits_zero(tmp_path, transport, capsys):
    transport.add("GET", f"/repos/{STORE}/contents/codex/lease.json?ref=main", status=500, text="boom")
    assert cli.main(
        ["credential-checkout", "--store", STORE, "--holder", "sdk-py#100",
         "--run-url", "https://run/1", "--codex-home", str(tmp_path / "h"), "--attempts", "1"]
    ) == 0
    assert "fetched=false" in capsys.readouterr().out


def test_checkin_deletes_the_credential_before_it_frees_the_lease(tmp_path, transport):
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    transport.add(
        "GET",
        f"/repos/{STORE}/contents/codex/lease.json?ref=main",
        data={
            "encoding": "base64",
            "content": base64.b64encode(json.dumps({"holder": "sdk-py#100"}).encode()).decode(),
            "sha": "b1",
        },
    )
    transport.add("PUT", f"/repos/{STORE}/contents/codex/lease.json", data={"commit": {}})
    assert cli.main(["credential-checkin", "--store", STORE, "--holder", "sdk-py#100", "--codex-home", str(home)]) == 0
    assert not home.exists()


def test_checkin_deletes_the_credential_even_when_the_store_is_unreachable(tmp_path, transport):
    # The file on the runner matters more than the lease record: the lease
    # expires by itself, a credential left on a persistent runner does not.
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    transport.add("GET", f"/repos/{STORE}/contents/codex/lease.json?ref=main", status=500, text="boom")
    assert cli.main(["credential-checkin", "--store", STORE, "--holder", "sdk-py#100", "--codex-home", str(home)]) == 0
    assert not home.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_checkout_command.py -v`
Expected: FAIL — `AttributeError: module 'reviewbot.cli' has no attribute '_store_api'`

- [ ] **Step 3: Write minimal implementation**

Add above `def main(` in `reviewbot/cli.py`:

```python
def _store_api(repo: str, token: str):
    """The store is a different repository, so it needs its own client.

    A seam, so the tests can inject a transport without reaching the network.
    """
    return GitHub(repo, token)


def _say_output(key: str, value: str) -> None:
    """Print for a human, and write for the workflow step that reads it."""
    print(f"{key}={value}")
    out_path = os.environ.get("GITHUB_OUTPUT")
    if out_path:
        with open(out_path, "a") as handle:
            handle.write(f"{key}={value}\n")


def credential_checkout(
    store: str, holder: str, run_url: str, codex_home: str, attempts: int, token: str
) -> int:
    """Take the lease and write the borrowed credential. Never fails the review.

    Every failure here reports `fetched=false` and exits 0. A job that cannot
    borrow the credential must review with Claude alone, not go red: a missing
    backend is already an ordinary outcome for `backends.run()`.
    """
    import datetime as _dt
    import time as _time
    from pathlib import Path

    from reviewbot import credentials

    api = _store_api(store, token)
    for attempt in range(1, max(1, attempts) + 1):
        try:
            if credentials.acquire(api, holder, run_url, _dt.datetime.now(_dt.UTC)):
                break
            print(f"reviewbot: the credential lease is held, attempt {attempt}/{attempts}")
        except GitHubError as exc:
            print(f"reviewbot: cannot reach the credential store: {scrub(str(exc))}")
            _say_output("fetched", "false")
            return 0
        if attempt == attempts:
            _say_output("fetched", "false")
            return 0
        _time.sleep(min(2**attempt, 30))

    try:
        text = api.file_at_ref(credentials.ISSUE_PATH, credentials.ISSUE_BRANCH)
    except GitHubError as exc:
        print(f"reviewbot: cannot read the issued credential: {scrub(str(exc))}")
        credentials.release(api, holder)
        _say_output("fetched", "false")
        return 0
    if not text:
        print("reviewbot: the store holds no issued credential")
        credentials.release(api, holder)
        _say_output("fetched", "false")
        return 0

    # Mask before writing: a later step that echoes the file must not leak it.
    try:
        for value in (json.loads(text).get("tokens") or {}).values():
            if isinstance(value, str) and value != credentials.PLACEHOLDER:
                print(f"::add-mask::{value}")
    except json.JSONDecodeError:
        pass

    home = Path(codex_home)
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    auth = home / "auth.json"
    auth.write_text(text)
    auth.chmod(0o600)
    _say_output("fetched", "true")
    return 0


def credential_checkin(store: str, holder: str, codex_home: str, token: str) -> int:
    """Delete the borrowed credential, then free the lease. Never fails.

    In that order, deliberately. The lease expires by itself after its TTL; a
    credential left behind on a persistent self-hosted runner does not.
    """
    import shutil as _shutil
    from pathlib import Path

    from reviewbot import credentials

    _shutil.rmtree(Path(codex_home), ignore_errors=True)
    try:
        credentials.release(_store_api(store, token), holder)
    except GitHubError as exc:
        print(f"reviewbot: could not free the lease, it will expire: {scrub(str(exc))}")
    return 0
```

Add the subparsers inside `main`, after the `deriver` block:

```python
    checkout = sub.add_parser("credential-checkout", help="borrow the Codex credential")
    checkout.add_argument("--store", required=True, help="owner/name of the credential store")
    checkout.add_argument("--holder", required=True, help="who is taking the lease")
    checkout.add_argument("--run-url", default="", help="the run that holds the lease")
    checkout.add_argument("--codex-home", required=True, help="where to write auth.json")
    checkout.add_argument("--attempts", type=int, default=4, help="lease attempts before giving up")

    checkin = sub.add_parser("credential-checkin", help="return the Codex credential")
    checkin.add_argument("--store", required=True, help="owner/name of the credential store")
    checkin.add_argument("--holder", required=True, help="who took the lease")
    checkin.add_argument("--codex-home", required=True, help="the directory to remove")
```

Add the dispatch inside `main`, after the `derive-credential` dispatch and
**after** the `token = os.environ.get(...)` check, because both commands need a
token:

```python
    if args.command == "credential-checkout":
        return credential_checkout(
            args.store, args.holder, args.run_url, args.codex_home, args.attempts, token
        )
    if args.command == "credential-checkin":
        return credential_checkin(args.store, args.holder, args.codex_home, token)
```

Note: `main` currently calls `parser.error("GITHUB_REPOSITORY is not set...")` when
`--repo` is missing. Move that check into the `gate`/`run` branches, because
these two commands take `--store` and never `--repo`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -v && uv run ruff check . && uv run ruff format --check .`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add reviewbot/cli.py tests/test_checkout_command.py
git commit -m "feat: check the Codex credential out and back in, never failing the review"
```

---

### Task 6: The Claude probe accepts an on-disk credential

**Files:**
- Modify: `reviewbot/backends/claude.py:22-24`
- Test: `tests/test_claude.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: no new names. `ClaudeBackend.probe()` changes meaning only.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_claude.py`:

```python
def test_claude_is_live_on_an_on_disk_credential_with_no_token(
    policy, bin_dir, monkeypatch, tmp_path
):
    # Measured 2026-09-16: with CLAUDE_CODE_OAUTH_TOKEN and ANTHROPIC_API_KEY
    # both unset, `claude -p` answered normally, reading
    # ~/.claude/.credentials.json. The old probe called that "not installed".
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    config = tmp_path / "claude-config"
    config.mkdir()
    (config / ".credentials.json").write_text("{}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    write_script(bin_dir, "claude", claude_script(VALID_JSON))
    assert base.build("claude", policy, str(tmp_path)).probe() is True


def test_claude_needs_a_credential_of_some_kind(policy, bin_dir, monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path / "empty"))
    write_script(bin_dir, "claude", claude_script(VALID_JSON))
    assert base.build("claude", policy, str(tmp_path)).probe() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_claude.py -k on_disk -v`
Expected: FAIL — `assert False is True`

- [ ] **Step 3: Write minimal implementation**

Replace `probe` in `reviewbot/backends/claude.py`:

```python
    def probe(self) -> bool:
        """Installed and authenticated, by token or by the credential on disk.

        The CLI needs no environment variable: measured 2026-09-16, `claude -p`
        answers with CLAUDE_CODE_OAUTH_TOKEN unset, reading
        ~/.claude/.credentials.json. Demanding the variable reported a working
        CLI as "not installed", which is why the self-hosted runner could not
        use the subscription already sitting on it.
        """
        if not shutil.which("claude"):
            return False
        if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            return True
        config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
            os.path.expanduser("~"), ".claude"
        )
        return os.path.exists(os.path.join(config, ".credentials.json"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_claude.py tests/test_modes.py -v && uv run ruff check .`
Expected: all PASS. `tests/test_modes.py` sets the token explicitly, so it is
unaffected.

- [ ] **Step 5: Commit**

```bash
git add reviewbot/backends/claude.py tests/test_claude.py
git commit -m "fix: the Claude probe called a working CLI 'not installed'"
```

---

### Task 7: Wire the workflow, inert until the store exists

**Files:**
- Modify: `.github/workflows/review.yml`
- Test: `tests/test_workflow.py`

**Interfaces:**
- Consumes: `credential-checkout`, `credential-checkin` (Task 5).
- Produces: workflow input `credential-store` (default
  `MarketData-App/code-review-credentials`); step outputs `codex.outputs.fetched`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_workflow.py`:

```python
# --- the borrowed Codex credential -----------------------------------------


def test_the_credential_is_borrowed_only_after_the_gate():
    # The store token is the org App token. Borrowing before the gate would
    # hand a credential to a job started by an outsider's pull request.
    names = [(s.get("name") or "") for s in steps()]
    gate = next(i for i, n in enumerate(names) if "Refuse a pull request" in n)
    borrow = next(i for i, n in enumerate(names) if "Borrow the Codex credential" in n)
    assert gate < borrow


def test_the_credential_is_written_to_a_job_scoped_codex_home():
    # The self-hosted runner is deliberately not --ephemeral, so its filesystem
    # persists between jobs. A credential written anywhere durable would wait
    # there for the next job, possibly from another repository.
    borrow = step("Borrow the Codex credential")
    assert "$RUNNER_TEMP/codex-home" in borrow["run"]
    assert "CODEX_HOME" in borrow["env"] or "CODEX_HOME" in borrow["run"]


def test_the_credential_is_returned_whatever_happens():
    give_back = step("Return the Codex credential")
    assert give_back["if"] == "always()"
    assert give_back is steps()[-1]


def test_the_codex_cli_is_installed_when_either_credential_is_present():
    install = step("Install the model CLIs")
    assert "steps.codex.outputs.fetched" in install["env"]["HAVE_CODEX"]
    assert "OPENAI_API_KEY" in install["env"]["HAVE_CODEX"]


def test_the_review_step_is_told_where_the_borrowed_credential_is():
    assert "codex-home" in step("Run the review")["env"]["CODEX_HOME"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_workflow.py -k credential -v`
Expected: FAIL — `AssertionError: no step named like Borrow the Codex credential`

- [ ] **Step 3: Write minimal implementation**

Add to `.github/workflows/review.yml` under `workflow_call.inputs`:

```yaml
      credential-store:
        description: >
          The private repository holding the Codex credential this job
          borrows. Read with the ORGANISATION App token, the same one the
          membership check uses, so a caller outside the organisation needs
          nothing extra. Set it to '' to turn borrowing off for a repository.
        type: string
        default: MarketData-App/code-review-credentials
```

Insert this step **after** `Check out the pull request head (read only)` and
**before** `Install the model CLIs`:

```yaml
      # BORROWED, NOT STORED. The real credential never leaves skynet; what
      # lands here is a copy whose refresh token is a placeholder, so this job
      # cannot refresh anything and cannot cause `refresh_token_reused`
      # however many reviews run at once. See
      # docs/superpowers/specs/2026-09-16-codex-credential-leasing-design.md.
      #
      # CODEX_HOME is under RUNNER_TEMP on purpose. The self-hosted runner is
      # not --ephemeral, so anything written to a durable path would wait
      # there for the next job, which may belong to another repository.
      - name: Borrow the Codex credential
        id: codex
        if: steps.gate.outputs.trusted == 'true' && inputs.credential-store != ''
        env:
          GITHUB_TOKEN: ${{ steps.org-token.outputs.token }}
        run: |
          uv run --project bot reviewbot credential-checkout \
            --store "${{ inputs.credential-store }}" \
            --holder "${{ github.repository }}#${{ steps.pr.outputs.number }}" \
            --run-url "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}" \
            --codex-home "$RUNNER_TEMP/codex-home"
```

Change the `Install the model CLIs` step's env:

```yaml
        env:
          HAVE_CLAUDE: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN != '' }}
          HAVE_CODEX: ${{ steps.codex.outputs.fetched == 'true' || secrets.OPENAI_API_KEY != '' }}
```

Add to the `Run the review` step's `env`:

```yaml
          CODEX_HOME: ${{ runner.temp }}/codex-home
```

Append as the **last** step in the job:

```yaml
      # The lease expires on its own after 20 minutes, so a job killed by
      # cancel-in-progress strands nothing. This step is still worth having:
      # it removes the credential from a runner whose filesystem persists, and
      # it frees the lease immediately rather than in twenty minutes.
      - name: Return the Codex credential
        if: always()
        env:
          GITHUB_TOKEN: ${{ steps.org-token.outputs.token }}
        run: |
          uv run --project bot reviewbot credential-checkin \
            --store "${{ inputs.credential-store }}" \
            --holder "${{ github.repository }}#${{ steps.pr.outputs.number }}" \
            --codex-home "$RUNNER_TEMP/codex-home" || true
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -v && uv run ruff check .`
Expected: all PASS. The workflow is inert until the store repository holds an
issued credential: `credential-checkout` reports `fetched=false` and the review
runs exactly as it does today.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/review.yml tests/test_workflow.py
git commit -m "feat: the review job borrows a Codex credential it cannot refresh"
```

---

### Task 8: Document it, and close the key-hygiene gap

**Files:**
- Modify: `README.md`, `docs/setup.md`, `.gitignore`
- Test: none — documentation, plus one ignore rule

**Interfaces:**
- Consumes: everything above.
- Produces: nothing code depends on.

- [ ] **Step 1: Add the ignore rule the repository's own rules require**

`CLAUDE.md` rule 1 says `.gitignore` must name every file that holds a secret.
The App's private key currently sits at
`.agentwatch/uploads/marketdata-code-review.2026-09-15.private-key.pem` inside
this working tree, protected only by the user's **global** ignore file.

```bash
printf '\n# Agent uploads. The App private key lands here; it must never be committed.\n.agentwatch/\n' >> .gitignore
git check-ignore -v .agentwatch/uploads/marketdata-code-review.2026-09-15.private-key.pem
```

Expected: the output now names `.gitignore`, not `~/.config/git/ignore`.

- [ ] **Step 2: Document the Codex credential in `docs/setup.md`**

Replace the `OPENAI_API_KEY` row of the secrets table with:

```markdown
| `OPENAI_API_KEY` | Optional. An API key for the Codex backend. A repository with no key borrows a credential instead; see "The Codex credential" below | The same two places |
```

And add a section after "3. Store the secrets":

```markdown
## 3a. The Codex credential

The Codex backend can run from a ChatGPT plan instead of an API key. A
personal plan authenticates with a file that holds a refresh token, and
OpenAI's CI guidance is explicit that one such file must not be shared across
concurrent jobs or machines: two processes redeeming one refresh token kill
the credential.

So no repository holds that file. One vault on skynet holds it, a keeper
publishes a copy whose refresh token is a placeholder, and each job borrows
that copy under a lease and deletes it afterwards. A borrowed copy can
authenticate and cannot refresh, so no number of concurrent reviews can
break the credential.

`docs/superpowers/specs/2026-09-16-codex-credential-leasing-design.md` is the
design, including what it costs and the condition that would replace it: a
ChatGPT Business or Enterprise workspace supports Codex access tokens, which
are finite and revocable, and would make all of this unnecessary.

Turn it off for one repository with `with: { credential-store: '' }`.
```

- [ ] **Step 3: Note the backend credentials in `README.md`**

Under "Calling it", after the `runs-on` line:

```markdown
The Codex backend needs no secret: the job borrows a short-lived credential
from a private store. Pass `credential-store: ''` to turn that off. A
repository that prefers an API key sets `OPENAI_API_KEY` and that takes
precedence.
```

- [ ] **Step 4: Verify the docs match the code**

Run: `uv run pytest -v`
Expected: PASS. `tests/test_workflow.py` asserts the `credential-store` input's
default, so a doc that names a different default fails here.

- [ ] **Step 5: Commit**

```bash
git add .gitignore README.md docs/setup.md
git commit -m "docs: the Codex backend borrows a credential, and the key gets an ignore rule"
```

---

## Self-review

**Spec coverage.** §4.2 the keeper → Task 4 (this repo's half; the timer and
the git push are the runner repo's plan, named in "Out of scope"). §4.3 the
store → Tasks 2, 3, 5. §4.4 the job, job-scoped `CODEX_HOME` and deletion →
Tasks 5 and 7. §5 the lease → Task 3. §6.1 workflow → Task 7. §6.2 Codex probe
→ no task needed; it already accepts `CODEX_HOME/auth.json`, which is why this
works unchanged. §6.3 Claude probe → Task 6. §6.4 `credentials.py` → Tasks 1
and 3. §7 failure modes → asserted in Tasks 3 and 5. §9 testing → every task.
§11 key hygiene → Task 8. §4.1 the vault and the `codex-keeper` tenant are the
runner repo's plan, by design.

**Gap found and closed.** An earlier draft had Task 5 release the lease before
deleting the credential. A store that is unreachable would then have left the
file on a persistent runner. Task 5 now deletes first and has a test for it.

**Type consistency.** `derive`, `access_token_expiry`, `acquire`, `release`,
`PLACEHOLDER`, `ISSUE_PATH`, `META_PATH`, `LEASE_PATH`, `ISSUE_BRANCH`,
`LEASE_BRANCH`, `file_with_sha`, `put_file`, `_store_api`, `_say_output` are
each defined in exactly one task and used with the same names and signatures
afterwards. `acquire` takes `now` as an argument rather than reading the clock,
so Task 3's tests are deterministic; the CLI in Task 5 supplies it.
