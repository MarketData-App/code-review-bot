# Codex credential leasing — design

Date: 2026-09-16. Status: design for review, not yet approved.

## 1. Purpose

Let the Codex backend run on **every** repository the bot reviews, including
the public `sdk-*` repositories on the MarketDataApp user account, using a
dedicated **personal** ChatGPT plan rather than an OpenAI API key.

A personal plan authenticates with a file, `$CODEX_HOME/auth.json`, that holds
a refresh token. That file cannot simply be copied to every runner: OpenAI
forbids it, and the failure it causes is the loss of the credential. This
design keeps exactly one copy of the refresh token, and lends every job a
derived copy that cannot refresh anything.

The Claude backend is fixed in passing: its probe demands an environment
variable the CLI itself does not need.

Non-goals: moving reviews off GitHub Actions, attaching a self-hosted runner
to a public repository, Codex access tokens (Business/Enterprise only), and
any change to the review's content or verdicts.

## 2. What was measured

Every number below was measured on 2026-09-16 on `taylor`, against the
credential in `~/.codex/auth.json` and Codex CLI 0.153.4, unless stated.

**The credential is portable and carries no machine binding.**
`auth.json` holds `auth_mode: chatgpt`, a null `OPENAI_API_KEY`, `last_refresh`,
and `tokens{id_token, access_token, refresh_token, account_id}`. Every JWT
claim was searched for a device id, machine id, MAC or fingerprint. None is
present. The claims identify an account: `chatgpt_account_id`,
`chatgpt_user_id`, `chatgpt_plan_type`. `~/.codex/installation_id` is a
separate file and is not part of the credential.

**The access token's life is about 10 days.** `last_refresh` was
2026-09-08T00:17Z; the `access_token` expires 2026-09-18T00:17Z. The
`id_token` had already expired, on 2026-09-08T01:17Z.

**A placeholder refresh token still authenticates.** A hand-written
`auth.json` whose `tokens.refresh_token` was replaced with a literal
placeholder, keeping the real access token, ran `codex exec` to completion:
exit 0, the model answered, 3,251 tokens used. `refresh_token` is a required
field for parsing, so it must be present; it need not be real.

**A stale `last_refresh` does not block a run.** The same file with
`last_refresh` set to 2026-08-01 — 46 days old, far past the documented ~8 day
threshold — also ran to completion, exit 0. So the binding constraint on a
derived copy is the **access token's own expiry**, not the refresh threshold.

**`codex login --device-auth` works headlessly.** It prints
`https://auth.openai.com/codex/device` and a one-time code that expires in 15
minutes. No browser is needed on the machine being logged in.

**`codex login --with-access-token` is not available to us.** Fed a real
ChatGPT access token it fails with `agent identity JWT payload is not valid
JSON`. That flag takes a Codex **access token**, an agent identity issued from
the ChatGPT admin console, and those are "currently supported for ChatGPT
Business and Enterprise workspaces". The plan bought for reviews is a personal
plan, so this path is closed. It is the path to revisit if the plan ever
changes; it would make this entire design unnecessary.

**Claude needs no secret.** With `CLAUDE_CODE_OAUTH_TOKEN` and
`ANTHROPIC_API_KEY` both unset, `claude -p` answered normally, reading
`~/.claude/.credentials.json`. Only `ClaudeBackend.probe()`
(`reviewbot/backends/claude.py:23`) requires the environment variable.

**The App can already reach a private store.** Both installations report
`repository_selection: all` — id 161969352 on the MarketData-App organisation
(with `contents: write`), and id 161969412 on the MarketDataApp user account.
An organisation installation token minted from the App key read a private org
repository's file: `GET .../self-hosted-runner/contents/README.md` returned
**HTTP 200**. `review.yml` already mints that token for the membership check.
No installation change is needed for a store repository that does not exist
yet.

**The App cannot write Actions secrets.** Its permission set is `checks`,
`contents`, `issues`, `members`, `metadata`, `pull_requests`. There is no
`secrets` permission, which rules out any design where a job rewrites a
repository secret.

**The runner image has no model CLI.** Inside `mda-runner-org` on skynet the
job user is `runner` (uid 1001); `node` and `npm` are present, `claude` and
`codex` are absent, `CODEX_HOME` is unset and `~/.codex` does not exist.

**The workflow cancels running reviews.** `review.yml` sets
`cancel-in-progress: true`. A push during a review kills the job. The longest
review observed to date took 5m46s (sdk-py run 35023147390).

## 3. What the vendor says, and why this design departs from it

OpenAI's CI/CD guidance is explicit:

> "Use one `auth.json` per runner or per serialized workflow stream. Do not
> share the same file across concurrent jobs or multiple machines."

> "Seed `auth.json` only if it is missing. If you rewrite the file from the
> original secret on every run, you throw away the refreshed tokens that Codex
> just wrote."

The failure this prevents has a name. When two processes redeem one refresh
token, the provider answers `refresh_token_reused` and the credential dies.
It is reported in the wild, across processes and across concurrent sessions
sharing one `~/.codex`.

This design distributes copies of `auth.json`, which the letter of that rule
forbids. It satisfies the rule's **purpose** instead: the copies it hands out
hold a placeholder refresh token, so no job can redeem a refresh token, so
`refresh_token_reused` cannot occur however many jobs run at once. The single
real refresh token stays on one machine, in one file, exactly as instructed.

This trade is stated here so that nobody has to re-derive it, and so that the
condition that reverses it is on the record: **if the review plan is ever
upgraded to a ChatGPT Business or Enterprise workspace, delete this design and
use a Codex access token.** That is one secret, finite by construction,
revocable from a console, and it needs no vault, no keeper and no lease.

## 4. Architecture

Four parts. Three of them are new.

```
  skynet, gh-runner tenant, docker volume
  ┌──────────────────────────────┐
  │ VAULT   auth.json            │  the only real refresh token,
  │         (real refresh token) │  created by device-auth, never copied
  └──────────────┬───────────────┘
                 │ daily, under flock
  ┌──────────────▼───────────────┐
  │ KEEPER  runs `codex exec`    │  lets Codex refresh itself,
  │         derives issue copy   │  then publishes a copy that cannot refresh
  └──────────────┬───────────────┘
                 │ org App installation token
  ┌──────────────▼───────────────┐
  │ STORE   MarketData-App/code-review-credentials (private)
  │   branch issue  codex/auth.json   the issue copy, force-pushed daily
  │   branch main   codex/lease.json  the lease, never touched by the keeper
  └──────────────┬───────────────┘
                 │ org App installation token (already minted in review.yml)
  ┌──────────────▼───────────────┐
  │ JOB     check out → review → check in
  │         ubuntu-latest or self-hosted, public or private, identical
  └──────────────────────────────┘
```

### 4.1 The vault

`codex login --device-auth`, run once on skynet, with `CODEX_HOME` pointing at
a Docker named volume. The real refresh token then exists in one file on one
machine, which is what OpenAI asks for.

The login is performed by a person: the command prints a code, the person
enters it at `https://auth.openai.com/codex/device` in any browser, signed in
as the **review** plan's account. Nothing is copied from `taylor`.

**The vault lives in its own tenant, not in the runner's.** It gets a new
unprivileged account on skynet — `codex-keeper` — created by that host's own
`/opt/scripts/create-agent-user.sh`, the same way `gh-runner` (1008),
`sdk-bot` (1007), `support-agent` (1006), `daily-standup` (1005) and `camofox`
(1004) were created. It runs its own rootless Docker daemon, holds the vault
volume, and runs the keeper container.

This separation is not tidiness, it is the whole security boundary. Were the
vault volume mounted into the **runner** container instead, any review job
could read the real refresh token at `$CODEX_HOME/auth.json`, and every claim
in §8 would be false. Two facts make the separation hold: the runner container
has **no docker socket** (verified: `/var/run/docker.sock` does not exist
inside `mda-runner-org`), so a job cannot reach another container or volume;
and `codex-keeper` is a different uid running a different daemon, so the
`gh-runner` tenant cannot read the vault either.

The runner container must never mount the vault volume. A reviewer of any
future change to `compose.yml` should treat a mount of it as a defect.

### 4.2 The keeper

A `systemd --user` timer in the `gh-runner` tenant, daily.

1. Take an `flock` on the volume, so it cannot race a review job on the same
   runner.
2. Run one trivial `codex exec`. This is deliberate: OpenAI's advice is "not
   to call the refresh API yourself, but rather to run Codex and persist the
   updated `auth.json`". Codex refreshes when it judges it necessary, and
   rewrites the vault in place.
3. Read the vault. Decode `tokens.access_token` and read its `exp` claim.
4. Build the issue copy: the same object, with `tokens.refresh_token` replaced
   by the literal string `REVIEWBOT-PLACEHOLDER-NOT-A-REFRESH-TOKEN`.
5. Publish the issue copy and its `exp` to the store.

**The keeper refuses to publish** when the access token expires within 48
hours and step 2 did not produce a newer one. It alarms instead, over the same
path the runner project already uses. Publishing a credential that will expire
mid-review is worse than publishing nothing, because a missing issue copy
degrades cleanly (§7) and an expiring one fails in the middle of a job.

### 4.3 The store

A new private repository, `MarketData-App/code-review-credentials`, holding
three files **on two branches**.

| Branch | File | Written by | How |
|---|---|---|---|
| `issue` | `codex/auth.json` | the keeper, daily | force-push, one commit |
| `issue` | `codex/meta.json` | the keeper, daily | the access token's `exp`, the publish time |
| `main` | `codex/lease.json` | every job | contents API, ordinary commits |

A repository rather than an Actions secret, for three reasons: the App has no
`secrets` permission; a secret cannot be read back by the keeper to check what
is published; and the contents API supplies the atomic primitive the lease
needs (§5). A repository rather than object storage, because the org App token
already reaches it and no new credential is introduced.

**Why two branches, and not one.** Every publish rewrites `codex/auth.json`,
so a single branch would accumulate expired access tokens in its history. The
keeper therefore keeps the issue copy on its own branch and force-pushes a
single commit each day, so no history accumulates. That force-push destroys
everything else on that branch, which is precisely why **the lease must not
live there** — a publish would erase a lease a running job still holds. The
lease stays on `main`, which the keeper never touches. This separation was the
first bug this design's own review found; it is written down so that nobody
consolidates the two branches later for tidiness.

The keeper force-pushes with `git`, not the contents API, which only makes
ordinary commits. The lease uses the contents API, because it needs that API's
compare-and-swap (§5).

### 4.4 The job

Unchanged in every respect except that it now borrows a credential. Public and
private repositories follow the identical path, which is the point of the
design: `sdk-py` on `ubuntu-latest` and `website` on skynet check out from the
same store with the same token. **A job on skynet uses the issue copy too.**
It never reads the vault, and cannot.

**`CODEX_HOME` is job-scoped.** The job sets it to `$RUNNER_TEMP/codex-home`,
a directory it creates, and never to a Docker volume or any path that outlives
the job. On `ubuntu-latest` this hardly matters; on the self-hosted runner it
matters a great deal, because that filesystem **persists between jobs** — the
runner is deliberately not `--ephemeral` (the runner project's
`docs/DESIGN.md` §"What persistence lets in" explains why). Writing the
credential to a persistent path would leave it for the next job, which may
belong to a different repository.

**The job deletes the credential when it finishes.** A step marked
`if: always()` removes `$CODEX_HOME` and releases the lease. It runs on
success, on failure, and on cancellation.

Be clear about what that deletion does and does not buy. It removes the file
from a persistent runner, which is real and worth having. It does **not**
shorten the access token's life: a copy read during the job is valid until the
token expires, whatever the job does afterwards. Deletion is hygiene, not
revocation. §8 states the exposure that remains.

## 5. The lease protocol

`codex/lease.json`:

```json
{
  "holder": "MarketDataApp/sdk-py#100",
  "run_url": "https://github.com/MarketDataApp/sdk-py/actions/runs/35023147390",
  "acquired_at": "2026-09-16T14:02:11Z",
  "expires_at": "2026-09-16T14:22:11Z"
}
```

**Check-out.** `GET /repos/.../contents/codex/lease.json?ref=main` returns the
content and its blob `sha`. If the lease is free, or `expires_at` is in the
past, the job PUTs a new body **with that `sha`**, naming `branch: main`. The
contents API rejects a PUT whose `sha` no longer matches with HTTP 409. That
is a genuine compare-and-swap: two jobs racing produce one winner and one 409,
with no lock service.

The loser retries with backoff for a bounded time.

**TTL is 20 minutes.** The longest review measured took 5m46s, so 20 minutes
is over three times the observed worst case and still short enough that a
killed job does not block the next review for long.

**Check-in.** A final step marked `if: always()` PUTs the lease back to free.
It runs after success, after failure, and after the review is cancelled,
provided the cancellation leaves the runner time to execute it.

**When check-in does not happen** — a hard kill, a runner that vanishes — the
lease simply expires. The next job sees `expires_at` in the past and takes it
over. No human action, no dead credential, no recovery runbook. This is the
property that the alternative design, leasing the *real* credential, cannot
have: there, a kill between refresh and write-back destroys the plan's access
until a person repeats device-auth.

**The lease is not required for correctness.** Nothing breaks if two jobs hold
the issue copy at once, because neither can refresh. The lease exists to keep
concurrent reviews from exhausting a personal plan's usage limits, and to make
"who has the credential out" answerable. It is therefore allowed to fail open,
which §7 describes.

## 6. Changes to existing code

### 6.1 `review.yml`

After the gate, before `Install the model CLIs`:

- **Check out the Codex credential.** Uses `steps.org-token.outputs.token`,
  which the workflow already mints. It acquires the lease on `main`, fetches
  `codex/auth.json` from the `issue` branch, writes it to
  `$CODEX_HOME/auth.json` at mode 600, and emits `::add-mask::` for each token
  value so no log can print one. It sets an output saying whether it
  succeeded.
- The existing install step changes its condition from "`OPENAI_API_KEY` is
  set" to "the check-out succeeded **or** `OPENAI_API_KEY` is set".
- **Check in the Codex credential**, last step, `if: always()`. It removes
  `$CODEX_HOME` and releases the lease, in that order, so a failure to reach
  the store still leaves no credential on the runner.

`OPENAI_API_KEY` stays supported and takes precedence. A repository that
prefers an API key is unaffected by any of this.

### 6.2 `reviewbot/backends/codex.py`

`probe()` already accepts `CODEX_HOME/auth.json`, so it needs no change. This
is why the design works with the engine as it stands.

### 6.3 `reviewbot/backends/claude.py`

`probe()` currently reads:

```python
return bool(shutil.which("claude")) and bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
```

It becomes the same shape as the Codex probe: the CLI, **and** either the
environment variable or an on-disk credential under `CLAUDE_CONFIG_DIR` or
`~/.claude`. Measured: the CLI authenticates from that file with no
environment variable set, so today's probe reports "not installed" for a
working CLI.

### 6.4 `reviewbot/credentials.py` — new

The derive-and-lease logic, as plain functions over the existing `GitHub`
client, which already injects its transport for testing:

- `derive(auth: dict) -> dict` — replace the refresh token, keep the rest.
- `access_token_expiry(auth: dict) -> datetime` — decode the `exp` claim.
- `acquire(api, holder, run_url, now, ttl) -> bool`
- `release(api, now) -> None`

## 7. Failure modes

| What fails | What happens |
|---|---|
| Lease is held by another job | Retry with backoff, then **skip Codex**. `backends.run()` already returns a backend that cannot run in `missing`, and the review proceeds with Claude. |
| Store is unreachable, or the issue copy is absent | Same: Codex is skipped, the review runs with Claude. |
| Issue copy's access token expired | `codex exec` fails. `backends.run()` collects the failure in `missing`; the review still publishes. |
| Job is cancelled mid-review | The lease expires after 20 minutes. Nothing else is affected. The vault is untouched, because the job never held a refresh token. |
| Keeper cannot refresh | It refuses to publish and alarms. Jobs keep using the last good issue copy until its access token expires, then degrade to Claude. |
| Both backends unavailable | Existing behaviour: the run reports on the check run as `neutral`, never as a red pull request. |

Every row degrades to "the review happens with Claude alone". None of them
loses the credential.

## 8. Security

**What is exposed.** The issue copy reaches a GitHub-hosted runner for public
repositories. It is a bearer credential for the review plan, usable until its
access token expires — up to about 10 days, typically 9 after a daily
republish. A personal plan offers nothing shorter-lived; this is the price of
not having a Business or Enterprise workspace.

**Deleting the file at the end of the job does not change that number.** The
job removes `$CODEX_HOME` (§4.4), which stops the credential outliving the job
on a persistent runner. It has no effect on the validity of a copy that was
already read. The two are separate risks and only one of them is fixed by
deletion.

**What is not exposed.** The refresh token never leaves skynet. Nothing that
leaks can be exchanged for a longer-lived credential, and nothing that leaks
can invalidate the vault's own token.

**What the job can see.** `pull_request_target` runs the workflow from the
base branch, and the org-only gate refuses an outsider before the pull
request's head is fetched. The model reads the head with read-only tools only.
So the credential is exposed to our own base-branch code, not to a
contributor's patch.

**Revocation is not instant.** Logging out on skynet invalidates the refresh
token, but an issue copy already published runs to its own expiry. To revoke
faster, publish an empty issue copy so new jobs find none, and accept that a
copy already fetched by a running job stays valid.

**Blast radius.** The plan is dedicated to code review. A leak costs review
usage on that plan, and reaches no other account or repository.

## 9. Testing

The project's rule stands: the default test run uses no network, no model and
no GitHub token.

- `derive()` — the refresh token is replaced, every other field is byte-identical,
  and the result still parses as Codex requires (`refresh_token` present).
- `access_token_expiry()` — a known JWT decodes to a known instant; a malformed
  token raises rather than returning a far-future date.
- `acquire()` / `release()` — free, held, expired, and the 409 race, all against
  the injected transport in the style of `tests/test_github.py`.
- The keeper's refusal to publish a nearly-expired token.
- `ClaudeBackend.probe()` — true with the environment variable, true with a
  credential file and no variable, false with neither.
- The existing opt-in `-m e2e` smoke gains one case that reviews a sandbox pull
  request with the Codex backend fed from a store.

## 10. Build order

1. `reviewbot/credentials.py` and its tests. No infrastructure needed.
2. The Claude probe fix and its tests. Independent of everything else.
3. Create the store repository. Verify the org token reads and writes it.
4. Add `claude` and `codex` to the runner image. The image has neither today.
5. Create the `codex-keeper` tenant on skynet with that host's own
   `/opt/scripts/create-agent-user.sh`, enable linger, and enable its rootless
   Docker daemon. Confirm it has no sudo and is not in the docker group, the
   same check `install.sh` already makes of `gh-runner`.
6. Device-auth the review plan into the vault volume, in the `codex-keeper`
   tenant. Confirm from inside `mda-runner-org` that the vault is **not**
   reachable.
7. The keeper: script, unit, timer, and its alarm path.
8. The workflow steps, behind the check-out succeeding, so the change is inert
   until the store holds something.
9. Turn it on for one private repository first, then `sdk-py`.

## 11. Separate from this design, and immediate

The App's private key is on `taylor` at
`.agentwatch/uploads/marketdata-code-review.2026-09-15.private-key.pem`,
**inside this repository's working tree**. It is not tracked and not in
history, but it is protected only by the user's global ignore file
(`~/.config/git/ignore`, `.agentwatch/`), not by this repository's own
`.gitignore`. Its mode is `664`.

`CLAUDE.md` rule 1 says: "No secret in git, ever… `.gitignore` names the known
ones; when you add a file that holds one, add its pattern in the same commit."

Two one-line fixes, owed regardless of whether this design is built: add
`.agentwatch/` to this repository's `.gitignore`, and `chmod 600` the key.

## 12. Open question for the reviewer

The keeper runs `codex exec` daily purely to let Codex refresh itself. That
costs a few thousand tokens a day against the review plan and is the only
part of this design that spends money for no review. The alternative is to
call the OAuth refresh endpoint directly, which OpenAI's guidance advises
against and which risks rotating into a broken state. The daily trivial run is
recommended; it is cheap, and it exercises the whole vault path once a day,
which is a health check worth having on its own.
