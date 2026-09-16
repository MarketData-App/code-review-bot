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

1. Copy `docs/caller-workflow.yml` from this repository to
   `.github/workflows/code-review.yml` in the target repository.
2. A private repository adds the self-hosted runner:

   ```yaml
       with:
         runs-on: '["self-hosted", "marketdata-docker"]'
   ```

3. Optional: add `.github/code-review/policy.yml` and
   `.github/code-review/REVIEW.md` on the default branch. Without them the
   repository gets the default review.

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
