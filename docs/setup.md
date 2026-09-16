# Setting up the code review bot

Creating a GitHub App is a human step. Everything here is done once, in a
browser and in the repository settings. It takes about ten minutes.

## 1. Create the GitHub App

Go to the **MarketData-App** organisation settings, **Developer settings**,
**GitHub Apps**, **New GitHub App**.

| Field | Value |
|---|---|
| GitHub App name | `marketdata-code-review` |
| Homepage URL | `https://github.com/MarketData-App/code-review-bot` |
| Webhook | **Uncheck Active.** The bot is driven by Actions, not by webhooks. |
| Where can this GitHub App be installed? | **Any account** |

The handle that appears on comments is `@marketdata-code-review[bot]`.

### Repository permissions

Set exactly these, and nothing else:

| Permission | Access | Why |
|---|---|---|
| Contents | **Read and write** | Arming and disarming native auto-merge. Nothing else writes. |
| Metadata | Read-only | Mandatory. |
| Pull requests | **Read and write** | The review comment, the approval, the labels. |
| Issues | **Read and write** | Issue comments on a pull request are issue comments. |
| Checks | **Read and write** | The check run and its annotations. |

Leave every other permission at **No access**. Subscribe to no events.

## 2. Install it

Install the App on both accounts, on **All repositories**:

1. The **MarketData-App** organisation.
2. The **MarketDataApp** user account, where the public `sdk-*` repositories
   live. This is the same cross-account pattern `marketdata-docs-sync`
   (app id 3771308) uses.

## 3. Store the secrets

Generate a private key on the App's page. It downloads as a `.pem` file.

**Only two values are genuinely secret.** The App's numeric id is not: an
unauthenticated `GET /apps/marketdata-code-review` returns it. It is a
workflow input with a default, so a repository outside the organisation has
one fewer value to copy.

| Secret name | Value | Where |
|---|---|---|
| `CODE_REVIEW_APP_PRIVATE_KEY` | The whole `.pem` file, header and footer included | The same two places |
| `CLAUDE_CODE_OAUTH_TOKEN` | From `claude setup-token` | The same two places |
| `OPENAI_API_KEY` | Optional. An API key for the Codex backend. Setting it turns borrowing off for that repository; a repository with no key borrows a credential instead. See "The Codex credential" below | The same two places |

**Why the MarketDataApp repositories need their own copies.** An organisation
secret can only be granted to repositories *in that organisation*, and the
`sdk-*` repositories are owned by the MarketDataApp user account, so they are
not in the set at all. There is no sharing mechanism across that boundary:
`secrets: inherit` does not help either, and not only for the reason it looks
like: it passes the *caller's* secrets, and it does so **only to a reusable
workflow in the same organisation**. A repository on the MarketDataApp user
account calling this one, owned by MarketData-App, crosses an owner boundary,
and `inherit` delivers nothing. The call then fails before any step runs, with
an error naming a secret that is sitting in the repository's settings:

```
Error when evaluating 'secrets' ... Secret CODE_REVIEW_APP_PRIVATE_KEY
is required, but not provided while calling
```

That is why `docs/caller-workflow.yml` names the secrets explicitly. The
explicit form works on both sides of the boundary; `inherit` works on one.

Two ways to avoid the duplication, both larger decisions:

- **Move the repository into MarketData-App.** Organisation secrets then
  reach it, and `uses:` can resolve a private bot repository once its Actions
  access is set to "Accessible from repositories in the organization" — so
  this repository would not need to be public at all.
- **Accept two secrets per SDK repository**, which is where things stand. For the organisation secrets, set the repository
access to the repositories that call the bot.

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

Three things stop a repository borrowing: `with: { credential-store: '' }` in
the caller, an `OPENAI_API_KEY` secret (which takes precedence), and a policy
that never reaches Codex. The last is the easy one to miss -- the shipped
default `backends: [claude, codex]` with `mode: first` runs Claude and never
invokes Codex, so the job declines the lease instead of holding one plan's
credential for a backend that will not run. A repository that wants Codex puts
it first, or sets `mode: all`.

**A repository with its own `OPENAI_API_KEY` never borrows.** The borrow step
is skipped when that secret is set, so such a repository takes no lease and no
borrowed `auth.json` reaches its runner. That is what makes the API key "take
precedence" a fact rather than a hope: the two credentials never meet, so the
Codex CLI is never asked to choose between them.

A credential the job cannot BORROW never fails a review. Every failure the borrow step
can name exits 0 and reports `fetched=false`; the step also carries a shell
fallback for the failures it cannot name, such as an empty organisation token.
A review that cannot borrow runs with Claude alone.

## 4. Turn it on for a repository

Six steps. Steps 3 and 4 are where every mistake has been made so far, so they
carry the measurement that found each one.

### 1. Install the App on the repository

`docs/setup.md` §2. Both installations already say `repository_selection: all`,
so a repository in `MarketData-App`, or on the `MarketDataApp` user account, is
covered the moment it exists. Nothing to do unless the repository is somewhere
else.

### 2. Add the two secrets

`CODE_REVIEW_APP_PRIVATE_KEY` and `CLAUDE_CODE_OAUTH_TOKEN`, on the repository.
An organisation secret does NOT reach a repository on the MarketDataApp user
account, which is where the `sdk-*` repositories live — that is the same owner
boundary that forces the caller to name its secrets explicitly rather than use
`secrets: inherit`.

### 3. Copy the caller workflow

Copy `docs/caller-workflow.yml` to `.github/workflows/code-review.yml` and
change the two things it marks. **Copy it; do not write your own from memory.**
Its comments carry four things that each cost a failed run:

- **Trigger on CI finishing, never on the push.** `pull_request_target` fires on
  `opened`/`synchronize`, which is when CI *starts*, and the bot refuses a commit
  whose checks are still running. Measured on sdk-py: tests take ~75 s, a review
  reaches that gate in 30–40 s. A push-triggered review skips and never returns,
  and the run reports success while doing nothing.
- **Name every CI workflow** in `workflow_run.workflows`, by its `name:` and not
  its filename. The gate looks at every check on the commit, so whichever
  finishes last is the one whose review proceeds. Earlier triggers skip cheaply;
  a repeat on the same head skips too. Extra triggers are self-deduplicating.
- **Only review pull requests.** `workflow_run` fires for default-branch pushes
  as well, because CI runs there too. Without
  `github.event.workflow_run.event == 'pull_request'`, every merge starts a job
  that mints App tokens, finds no pull request and does nothing.
- **`force: ${{ inputs.force || false }}`.** `inputs` exists only for
  `workflow_dispatch`. On any other event it is null, and a null against a
  boolean-typed input fails the *whole workflow at evaluation* — no job, no log,
  just "this run likely failed because of a workflow file issue".

If you pin `uses:` to a branch or tag, pin `bot-ref` to the **same** ref.
`uses:` picks the workflow; `bot-ref` picks the bot code it checks out, and its
default is `main`. A mismatch fails with `invalid choice` on whichever command
the older side lacks.

### 3a. The CI workflow must be named `Tests`

The trigger matches a workflow by its `name:`, so the name is an interface. All
five SDK repositories now use `Tests`, which is what lets one caller file be
copied without edits.

They did not start that way — `Tests` in sdk-go, sdk-php and sdk-py, `CI` in
sdk-js, `Pull Request` in sdk-java — and naming the wrong one **fails silently**:
no review ever runs and nothing says why. sdk-java is the trap, because its
`Main` workflow looks like the obvious candidate and only runs on pushes to the
default branch.

**Find the right workflow empirically, not from filenames.** Look at which
checks actually appear on an open pull request:

```bash
sha=$(gh api repos/<owner>/<repo>/pulls/<n> --jq .head.sha)
gh api "repos/<owner>/<repo>/commits/$sha/check-runs" --jq '.check_runs[].name'
```

then find which workflow produces them. Rename that workflow to `Tests`.

**Renaming is safe.** Every required status check in these repositories is a
**job** name, not a workflow name — `test (3.10)`, `Verify (JDK 17)`,
`check (20.x)` — so branch protection is untouched. The only thing that moves is
the label in the Actions list.

### 4. Add a policy, if the defaults are not what you want

`.github/code-review/policy.yml` on the **base** branch. Absent keys keep the
defaults in `reviewbot/defaults/policy.yml`. Three choices actually matter:

**Which model reviews.** `backends` is a LIST, so naming it replaces the default
wholesale rather than adding to it.

| you want | write |
|---|---|
| Claude only | `backends: [claude]` |
| Codex, falling back to Claude | `backends: [codex, claude]` + `mode: fallback` |
| both, merged | `backends: [claude, codex]` + `mode: all` |

Use `fallback`, not `first`, when you list two. Under `first` the run is
`[ready[0].review(brief)]` and a `BackendError` goes straight to `_fail`: the
second backend is never tried, because it was dropped into `missing` the moment
the first probed live. Since `CodexBackend.probe()` returns true as soon as
`$CODEX_HOME/auth.json` *exists*, a stale borrowed credential is live-but-broken
— exactly the state that would end the review with a neutral "Bot error" and no
comment.

**Codex must be FIRST to get the credential.** `credential-checkout` declines the
shared lease when codex is not first under `first` or `fallback`, because a later
backend is reached rarely or never and holding one plan's lease for it starves
the repositories that use it on every run. So `[claude, codex]` with
`mode: fallback` gets no Codex credential, by design.

**The proof gate is off by default and should usually stay off.** It warns: the
model still judges the evidence and asks for it under "Before merge". Turning it
on makes missing evidence *overwrite the verdict*. Measured on the first three
real reviews: sdk-py #122 scored patch 6/6 with zero findings and was still
reported Blocked, and all three would have been. If you do turn it on, pair it
with `proof.paths` — and know that it is an ANY-match over the whole pull
request, so one matching file gates every file. On sdk-py,
`paths: ["src/marketdata/**"]` fires on 7 of 7 open pull requests.

### 5. Leave the check advisory until you trust it

The `Code review` check is not a required status check anywhere today, so a
⛔ blocks no merges. Add it to a branch rule only once you have read a few of its
reviews and agree with them.

### 6. Prove it, do not assume it

```bash
gh workflow run code-review.yml -R <owner>/<repo> -f pr=<number>
```

Then read the run's log, not just its colour. A green run that skipped is the
outcome that hides: look for `reviewbot: blocked on …` or `reviewbot: ready …`
rather than a skip line. `reviewbot: skipped, …` names its reason — CI not
finished, already reviewed, every file in `ignore_paths`.

To re-review a commit deliberately, pass `-f force=true`, or comment
`@marketdata-code-review re-review` on the pull request.

## 5. Optional repository settings

- **Labels.** The bot creates none. Create these four so they carry a colour:
  `review: ready`, `review: needs changes`, `review: needs proof`,
  `review: decision needed`. A maintainer applies `review: proof waived` to
  lift the proof gate where a repository has enabled it; the bot honours the
  label and never sets or clears it. **The gate is off by default**: missing
  runtime evidence is reported and asked for, not enforced. Turn it on per
  repository with `proof: {required: true}`, and pair it with `proof.paths` so
  it stays a floor under the model's judgement rather than a blanket.
- **Auto-merge.** Only needed when `policy.auto_merge.enabled` is true. Turn
  on **Allow auto-merge** in the repository settings, and add a branch rule
  that requires the `Code review` check and the repository's CI.
- **Required check.** To make the review a merge gate, add the check named by
  `policy.check_name` (default `Code review`) to the branch protection rule.

## 6. Check that it works

Open a pull request with a small behaviour change and no evidence in the
description. Within a few minutes it should get one comment, a `Code review`
check run, and a `review: needs proof` label. If nothing appears, open the
Actions tab of the target repository: a bot failure always fails the job, and
its check run says what went wrong.
